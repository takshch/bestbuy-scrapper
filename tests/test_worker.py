"""End-to-end worker runs against Postgres with the browser replaced by a fake."""
import asyncio
import threading
import time

import pytest
from sqlalchemy import select

from app.models import Product, ScrapeJob, ScrapeTask, Worker
from app.queue import enqueue_job
from app.worker import ScrapeWorker
from tests.conftest import make_settings

pytestmark = pytest.mark.db


class FakeScraper:
    """Thread-safe fake of scrape_page that records peak parallelism."""

    def __init__(self, delay=0.3, fail_pages=(), last_page=None):
        self.delay, self.fail_pages, self.last_page = delay, set(fail_pages), last_page
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.calls = []

    def __call__(self, query, page, settings, start_with_proxy):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(page)
        try:
            time.sleep(self.delay)
            if page in self.fail_pages:
                raise RuntimeError("browser crashed")
            if self.last_page is not None and page > self.last_page:
                return {"route": "direct", "cards": 0, "products": [], "errors": []}
            return {"route": "direct", "fell_back": False, "proxy_failed": False, "cards": 2, "errors": [],
                    "products": [{"sku": f"{query}-{page}-{i}", "name": f"{query} {page}.{i}", "price": 10.0 + i}
                                 for i in range(2)]}
        finally:
            with self.lock:
                self.active -= 1


async def run_until_idle(db, worker, job_ids, timeout=30):
    runner = asyncio.create_task(worker.run())
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            async with db() as s:
                statuses = (await s.execute(select(ScrapeJob.status).where(ScrapeJob.id.in_(job_ids)))).scalars().all()
            if statuses and all(st in ("done", "failed", "cancelled") for st in statuses):
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"jobs not finished: {statuses}")
    finally:
        worker.stop()
        await asyncio.wait_for(runner, 30)


def fast_settings(**kw):
    return make_settings(worker_poll_interval=0.1, task_lease_seconds=60, **kw)


async def test_worker_scrapes_pages_in_parallel(db):
    async with db() as s:
        jobs = [await enqueue_job(s, "tv", 4), await enqueue_job(s, "laptop", 4)]
    fake = FakeScraper()
    worker = ScrapeWorker(fast_settings(), concurrency=4, scrape=fake)

    started = time.monotonic()
    await run_until_idle(db, worker, [j.id for j in jobs])
    elapsed = time.monotonic() - started

    assert sorted(fake.calls) == [1, 1, 2, 2, 3, 3, 4, 4]
    assert fake.peak == 4, "should run 4 pages at once"
    assert elapsed < 8 * fake.delay, "parallel run should beat serial time"
    async with db() as s:
        assert {j.status for j in (await s.execute(select(ScrapeJob))).scalars()} == {"done"}
        assert len((await s.execute(select(Product))).scalars().all()) == 16
        w = (await s.execute(select(Worker))).scalar_one()
        assert w.stopped_at is not None and w.concurrency == 4


async def test_two_workers_share_the_queue(db):
    async with db() as s:
        job = await enqueue_job(s, "tv", 8)
    fake_a, fake_b = FakeScraper(), FakeScraper()
    a = ScrapeWorker(fast_settings(), concurrency=2, scrape=fake_a)
    b = ScrapeWorker(fast_settings(), concurrency=2, scrape=fake_b)
    await asyncio.gather(run_until_idle(db, a, [job.id]), run_until_idle(db, b, [job.id]))

    assert sorted(fake_a.calls + fake_b.calls) == list(range(1, 9)), "each page scraped exactly once"
    assert fake_a.calls and fake_b.calls, "both workers took work"
    async with db() as s:
        workers = {t.worker_id for t in (await s.execute(select(ScrapeTask))).scalars()}
    assert workers == {a.id, b.id}


async def test_crash_is_retried_and_job_still_finishes(db):
    async with db() as s:
        job = await enqueue_job(s, "tv", 3)
    fake = FakeScraper(fail_pages={2})
    worker = ScrapeWorker(fast_settings(task_max_attempts=2), concurrency=2, scrape=fake)
    await run_until_idle(db, worker, [job.id])

    assert fake.calls.count(2) == 2, "crashed page retried once"
    async with db() as s:
        tasks = (await s.execute(select(ScrapeTask).order_by(ScrapeTask.page))).scalars().all()
        job = await s.get(ScrapeJob, job.id)
    assert [t.status for t in tasks] == ["done", "failed", "done"]
    assert job.status == "done" and job.products_found == 4 and "browser crashed" in job.error


async def test_stops_at_last_page_of_results(db):
    async with db() as s:
        job = await enqueue_job(s, "rare item", 10)
    fake = FakeScraper(last_page=2, delay=0.05)
    worker = ScrapeWorker(fast_settings(), concurrency=1, scrape=fake)
    await run_until_idle(db, worker, [job.id])

    assert fake.calls == [1, 2, 3], "page 3 is empty, pages 4-10 are skipped"
    async with db() as s:
        job = await s.get(ScrapeJob, job.id)
    assert (job.status, job.last_page, job.products_found) == ("done", 2, 4)


async def test_graceful_stop_finishes_in_flight_pages_and_claims_no_more(db):
    async with db() as s:
        job = await enqueue_job(s, "tv", 6)
    fake = FakeScraper(delay=1.0)
    worker = ScrapeWorker(fast_settings(), concurrency=2, scrape=fake)
    runner = asyncio.create_task(worker.run())
    await asyncio.sleep(0.5)  # both slots are mid-page
    worker.stop()
    await asyncio.wait_for(runner, 10)

    async with db() as s:
        tasks = (await s.execute(select(ScrapeTask).order_by(ScrapeTask.page))).scalars().all()
        w = (await s.execute(select(Worker))).scalar_one()
    assert [t.status for t in tasks] == ["done", "done", "queued", "queued", "queued", "queued"]
    assert sorted(fake.calls) == [1, 2] and w.stopped_at is not None
