"""Postgres-backed work queue for scrape jobs.

A job is split into one task per results page. Workers claim tasks with
``SELECT ... FOR UPDATE SKIP LOCKED``, so any number of worker processes can pull
from the queue concurrently without double-processing. Running tasks hold a lease
renewed by heartbeats; tasks whose worker died are requeued by ``reap_stale_tasks``.

Lock ordering (prevents deadlocks between concurrent workers): a transaction that
needs both locks the job row first, then task rows. Claiming touches only task rows.
Note that inserting rows with a job_id FK takes a KEY SHARE lock on the job, so the
job's FOR UPDATE lock must be taken *before* those inserts, not after.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RouteState, ScrapeJob, ScrapeTask, Worker
from app.storage import save_products

log = logging.getLogger("queue")

ACTIVE_TASK = ("queued", "running")
MAX_JOB_ERRORS = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ClaimedTask:
    id: int
    job_id: int
    page: int
    attempts: int
    query: str


# --------------------------------------------------------------------------- jobs

async def enqueue_job(session: AsyncSession, query: str, max_pages: int) -> ScrapeJob:
    job = ScrapeJob(query=query, max_pages=max_pages, status="pending", products_found=0, pages_done=0)
    session.add(job)
    await session.flush()
    session.add_all(ScrapeTask(job_id=job.id, page=p, status="queued") for p in range(1, max_pages + 1))
    await session.commit()
    await session.refresh(job)
    return job


async def cancel_job(session: AsyncSession, job_id: int) -> Optional[ScrapeJob]:
    """Skip the job's queued pages; pages already running finish normally."""
    job = await session.get(ScrapeJob, job_id, with_for_update=True)
    if job is None:
        return None
    if job.status in ("pending", "running"):
        await session.execute(
            update(ScrapeTask)
            .where(ScrapeTask.job_id == job_id, ScrapeTask.status == "queued")
            .values(status="skipped", finished_at=_now())
        )
        job.status = "cancelled"
        await session.flush()
        await finalize_job(session, job_id)
    await session.commit()
    await session.refresh(job)
    return job


async def finalize_job(session: AsyncSession, job_id: int) -> None:
    """Recompute a job's progress from its tasks; close it when no work is left."""
    job = await session.get(ScrapeJob, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        return
    tasks = (await session.execute(select(ScrapeTask).where(ScrapeTask.job_id == job_id))).scalars().all()

    done = [t for t in tasks if t.status == "done"]
    job.pages_done = len(done)
    job.products_found = sum(t.products_found for t in done)
    routes = sorted({t.route for t in tasks if t.route})
    job.route = "; ".join(routes) or None
    errors: List[Dict[str, Any]] = []
    for t in sorted(tasks, key=lambda t: t.page):
        errors.extend(json.loads(t.errors) if t.errors else [])
    job.error = json.dumps(errors[:MAX_JOB_ERRORS]) if errors else None

    if any(t.status in ACTIVE_TASK for t in tasks):
        if job.status == "pending" and any(t.status != "queued" for t in tasks):
            job.status = "running"
        return
    if job.status != "cancelled":
        job.status = "done" if done else "failed"
    job.finished_at = job.finished_at or _now()


# --------------------------------------------------------------------------- tasks

_CLAIM_SQL = text(
    """
    WITH next AS (
        SELECT t.id
        FROM scrape_tasks t
        JOIN scrape_jobs j ON j.id = t.job_id
        WHERE t.status = 'queued'
          AND j.status IN ('pending', 'running')
          AND (j.last_page IS NULL OR t.page <= j.last_page)
        -- Page-first ordering round-robins between jobs, so one huge job can't starve others.
        ORDER BY t.page, t.id
        FOR UPDATE OF t SKIP LOCKED
        LIMIT 1
    )
    UPDATE scrape_tasks t
    SET status = 'running', worker_id = :worker_id, attempts = t.attempts + 1,
        heartbeat_at = now(), started_at = now(), finished_at = NULL
    FROM next
    WHERE t.id = next.id
    RETURNING t.id, t.job_id, t.page, t.attempts
    """
)


async def claim_task(session: AsyncSession, worker_id: str) -> Optional[ClaimedTask]:
    row = (await session.execute(_CLAIM_SQL, {"worker_id": worker_id})).first()
    await session.commit()  # the claim transaction touches task rows only
    if row is None:
        return None
    # Separate transaction: mark the job started (no-op unless it is still pending).
    await session.execute(
        update(ScrapeJob)
        .where(ScrapeJob.id == row.job_id, ScrapeJob.status == "pending")
        .values(status="running", started_at=func.now())
    )
    await session.commit()
    query = await session.scalar(select(ScrapeJob.query).where(ScrapeJob.id == row.job_id))
    return ClaimedTask(id=row.id, job_id=row.job_id, page=row.page, attempts=row.attempts, query=query)


async def complete_task(
    session: AsyncSession,
    task: ClaimedTask,
    worker_id: str,
    result: Dict[str, Any],
    max_attempts: int,
) -> str:
    """Store a page result. Returns the task's new status, or "lost" if our lease expired
    and the task was reassigned (the result is then discarded)."""
    # Job first, then task (see module docstring on lock ordering).
    job = await session.get(ScrapeJob, task.job_id, with_for_update=True, populate_existing=True)
    owned = await session.get(ScrapeTask, task.id, with_for_update=True, populate_existing=True)
    if job is None or owned is None or owned.status != "running" or owned.worker_id != worker_id:
        await session.rollback()
        log.warning("task %s: lease lost, discarding result", task.id)
        return "lost"

    products = result.get("products") or []
    errors = result.get("errors") or []
    saved = await save_products(session, products, task.query, task.job_id) if products else 0

    no_results = result.get("cards", 0) == 0 and not errors
    failed = saved == 0 and bool(errors)
    if failed and task.attempts < max_attempts:
        status = "queued"  # retry later, possibly via another worker/route
    else:
        status = "failed" if failed else "done"

    owned.status = status
    owned.route = (result.get("route") or "")[:255] or None
    owned.fell_back = bool(result.get("fell_back"))
    owned.products_found = saved
    owned.errors = json.dumps(errors) if errors else None
    owned.worker_id = None if status == "queued" else worker_id
    owned.finished_at = None if status == "queued" else _now()

    if no_results:
        # Past the last page of results: skip the rest of this job's pages.
        last = task.page - 1
        if job.last_page is None or last < job.last_page:
            job.last_page = last
        await session.execute(
            update(ScrapeTask)
            .where(ScrapeTask.job_id == task.job_id, ScrapeTask.page > last, ScrapeTask.status == "queued")
            .values(status="skipped", finished_at=_now())
        )

    await session.flush()
    await finalize_job(session, task.job_id)
    await session.commit()
    return status


async def release_task(session: AsyncSession, task: ClaimedTask, worker_id: str, reason: str) -> None:
    """Hand a task back to the queue (e.g. on worker shutdown or an unexpected crash)."""
    await session.execute(
        update(ScrapeTask)
        .where(ScrapeTask.id == task.id, ScrapeTask.worker_id == worker_id, ScrapeTask.status == "running")
        .values(
            status="queued",
            worker_id=None,
            errors=json.dumps([{"page": task.page, "route": None, "error": reason}]),
        )
    )
    await session.commit()


async def reap_stale_tasks(session: AsyncSession, lease_seconds: int, max_attempts: int) -> int:
    """Requeue (or fail, after max attempts) running tasks whose worker stopped heartbeating."""
    cutoff = _now() - timedelta(seconds=lease_seconds)
    rows = (
        await session.execute(
            text(
                """
                UPDATE scrape_tasks
                SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
                    worker_id = NULL,
                    finished_at = CASE WHEN attempts >= :max_attempts THEN now() ELSE NULL END,
                    errors = :error
                WHERE status = 'running' AND heartbeat_at < :cutoff
                RETURNING job_id
                """
            ),
            {
                "max_attempts": max_attempts,
                "cutoff": cutoff,
                "error": json.dumps([{"page": None, "route": None, "error": "worker lease expired"}]),
            },
        )
    ).all()
    await session.commit()  # release task locks before taking job locks
    for job_id in sorted({r.job_id for r in rows}):
        await finalize_job(session, job_id)
        await session.commit()
    if rows:
        log.warning("requeued/failed %s stale task(s)", len(rows))
    return len(rows)


# --------------------------------------------------------------------------- workers

async def register_worker(session: AsyncSession, worker_id: str, hostname: str, concurrency: int) -> None:
    stmt = insert(Worker).values(id=worker_id, hostname=hostname, concurrency=concurrency, busy=0)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Worker.id],
        set_={"hostname": hostname, "concurrency": concurrency, "heartbeat_at": func.now(), "stopped_at": None},
    )
    await session.execute(stmt)
    await session.commit()


async def heartbeat(session: AsyncSession, worker_id: str, busy: int, done: int, failed: int) -> None:
    await session.execute(
        update(ScrapeTask)
        .where(ScrapeTask.worker_id == worker_id, ScrapeTask.status == "running")
        .values(heartbeat_at=func.now())
    )
    await session.execute(
        update(Worker)
        .where(Worker.id == worker_id)
        .values(heartbeat_at=func.now(), busy=busy, tasks_done=done, tasks_failed=failed)
    )
    await session.commit()


async def worker_stopped(session: AsyncSession, worker_id: str) -> None:
    await session.execute(update(Worker).where(Worker.id == worker_id).values(stopped_at=func.now(), busy=0))
    await session.commit()


# --------------------------------------------------------------------------- routing state

DIRECT = "direct"


async def direct_cooldown_remaining(session: AsyncSession) -> int:
    # Query (not session.get): the identity map could return a stale row.
    blocked_until = await session.scalar(select(RouteState.blocked_until).where(RouteState.name == DIRECT))
    if blocked_until is None:
        return 0
    return max(0, int((blocked_until - _now()).total_seconds()))


async def update_direct_cooldown(
    session: AsyncSession, result: Dict[str, Any], started_with_proxy: bool, retry_after: int
) -> None:
    """Shared across workers: after the direct IP fails and the proxy works, route new
    pages proxy-first for ``retry_after`` seconds; a clean direct page clears it."""
    blocked_until: Optional[datetime]
    if result.get("fell_back") and not result.get("proxy_failed") and not started_with_proxy and retry_after > 0:
        blocked_until = _now() + timedelta(seconds=retry_after)
        reason = (result.get("errors") or [{}])[0].get("error")
        log.warning("direct IP blocked; routing proxy-first for %ss", retry_after)
    elif (result.get("route") or "").endswith(DIRECT) and result.get("products") and not result.get("errors"):
        blocked_until, reason = None, None
    else:
        return
    stmt = insert(RouteState).values(name=DIRECT, blocked_until=blocked_until, reason=reason)
    stmt = stmt.on_conflict_do_update(
        index_elements=[RouteState.name],
        set_={"blocked_until": blocked_until, "reason": reason, "updated_at": func.now()},
    )
    await session.execute(stmt)
    await session.commit()
