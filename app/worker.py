"""Scrape worker process.

Run one or more of these alongside the API:

    python -m app.worker                    # uses WORKER_CONCURRENCY browser slots
    docker compose up --scale worker=4      # horizontal scaling

Each slot claims one page task at a time from Postgres, scrapes it in a thread
(Selenium is blocking), stores the result, and repeats. A heartbeat loop renews
the leases of running tasks; a reaper loop requeues tasks of dead workers.
"""
import asyncio
import logging
import os
import secrets
import signal
import socket
from typing import Dict, Optional

from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.queue import (
    ClaimedTask,
    claim_task,
    complete_task,
    direct_cooldown_remaining,
    heartbeat,
    reap_stale_tasks,
    register_worker,
    release_task,
    update_direct_cooldown,
    worker_stopped,
)
from app.scraping.bestbuy import scrape_page

log = logging.getLogger("worker")


class ScrapeWorker:
    def __init__(self, settings: Settings, concurrency: Optional[int] = None, scrape=scrape_page):
        self.settings = settings
        self.concurrency = concurrency or settings.worker_concurrency
        self.scrape = scrape  # injectable for tests
        self.id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
        self.stopping = asyncio.Event()
        self.running: Dict[int, ClaimedTask] = {}
        self.done = 0
        self.failed = 0

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        async with SessionLocal() as s:
            await register_worker(s, self.id, socket.gethostname(), self.concurrency)
        log.info("worker %s started with %s slot(s)", self.id, self.concurrency)

        slots = [asyncio.create_task(self._slot(i)) for i in range(self.concurrency)]
        background = [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self._reaper_loop())]
        await self.stopping.wait()

        log.info("worker %s stopping; waiting for %s running task(s)", self.id, len(self.running))
        # Slots finish their current page; give up after the lease period and requeue.
        _, pending = await asyncio.wait(slots, timeout=self.settings.task_lease_seconds)
        for t in pending:
            t.cancel()
        async with SessionLocal() as s:
            for task in list(self.running.values()):
                await release_task(s, task, self.id, "worker shut down before finishing")
            await worker_stopped(s, self.id)
        for t in background:
            t.cancel()
        log.info("worker %s stopped (done=%s failed=%s)", self.id, self.done, self.failed)

    def stop(self) -> None:
        self.stopping.set()

    # ------------------------------------------------------------------ loops

    async def _slot(self, slot: int) -> None:
        while not self.stopping.is_set():
            try:
                async with SessionLocal() as s:
                    task = await claim_task(s, self.id)
                if task is None:
                    await self._sleep(self.settings.worker_poll_interval)
                    continue
                await self.process(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the slot alive (DB blip, etc.)
                log.exception("slot %s: unexpected error", slot)
                await self._sleep(5)

    async def process(self, task: ClaimedTask) -> str:
        self.running[task.id] = task
        try:
            async with SessionLocal() as s:
                start_with_proxy = await direct_cooldown_remaining(s) > 0
            log.info("task %s: %r page %s (attempt %s%s)", task.id, task.query, task.page,
                     task.attempts, ", proxy-first" if start_with_proxy else "")
            try:
                result = await asyncio.to_thread(
                    self.scrape, task.query, task.page, self.settings, start_with_proxy
                )
            except Exception as exc:  # noqa: BLE001 - a crash in one page must not kill the slot
                log.exception("task %s crashed", task.id)
                result = {"route": None, "cards": 0, "products": [],
                          "errors": [{"page": task.page, "route": None, "error": f"crash: {exc}"}]}

            async with SessionLocal() as s:
                status = await complete_task(s, task, self.id, result, self.settings.task_max_attempts)
                await update_direct_cooldown(s, result, start_with_proxy, self.settings.direct_retry_after)
            if status == "done":
                self.done += 1
            elif status in ("failed", "queued"):
                self.failed += 1
            log.info("task %s: %s via %s, %s products", task.id, status, result.get("route"),
                     len(result.get("products") or []))
            return status
        finally:
            self.running.pop(task.id, None)

    async def _heartbeat_loop(self) -> None:
        interval = max(5, self.settings.task_lease_seconds // 4)
        while True:
            try:
                async with SessionLocal() as s:
                    await heartbeat(s, self.id, len(self.running), self.done, self.failed)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")
            await asyncio.sleep(interval)

    async def _reaper_loop(self) -> None:
        interval = max(10, self.settings.task_lease_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            try:
                async with SessionLocal() as s:
                    await reap_stale_tasks(s, self.settings.task_lease_seconds, self.settings.task_max_attempts)
            except Exception:  # noqa: BLE001
                log.exception("reaper failed")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = ScrapeWorker(get_settings())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
