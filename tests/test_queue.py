"""Postgres queue semantics: claiming, concurrency, retries, leases, cancellation."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app import queue as q
from app.models import PriceHistory, Product, RouteState, ScrapeJob, ScrapeTask

pytestmark = pytest.mark.db

W1, W2 = "worker-1", "worker-2"


def page_result(n_products=3, route="direct", errors=None, cards=None, price=9.99, prefix="sku"):
    products = [{"sku": f"{prefix}{i}", "name": f"Product {i}", "price": price, "regular_price": None}
                for i in range(n_products)]
    return {"route": route, "fell_back": False, "proxy_failed": False,
            "cards": n_products if cards is None else cards, "products": products, "errors": errors or []}


async def enqueue(db, query="tv", pages=3):
    async with db() as s:
        return await q.enqueue_job(s, query, pages)


async def claim(db, worker=W1):
    async with db() as s:
        return await q.claim_task(s, worker)


async def complete(db, task, result, worker=W1, max_attempts=2):
    async with db() as s:
        return await q.complete_task(s, task, worker, result, max_attempts)


async def get_job(db, job_id):
    async with db() as s:
        return await s.get(ScrapeJob, job_id)


async def tasks_of(db, job_id):
    async with db() as s:
        return (await s.execute(select(ScrapeTask).where(ScrapeTask.job_id == job_id)
                                .order_by(ScrapeTask.page))).scalars().all()


async def test_enqueue_creates_one_task_per_page(db):
    job = await enqueue(db, pages=4)
    assert job.status == "pending"
    assert [(t.page, t.status) for t in await tasks_of(db, job.id)] == [(p, "queued") for p in range(1, 5)]


async def test_claim_marks_running_and_starts_job(db):
    job = await enqueue(db)
    task = await claim(db)
    assert (task.job_id, task.page, task.attempts, task.query) == (job.id, 1, 1, "tv")
    assert (await get_job(db, job.id)).status == "running"
    running = (await tasks_of(db, job.id))[0]
    assert running.status == "running" and running.worker_id == W1 and running.heartbeat_at


async def test_claim_round_robins_between_jobs(db):
    a = await enqueue(db, "a", pages=3)
    b = await enqueue(db, "b", pages=3)
    claimed = [await claim(db) for _ in range(4)]
    assert [(t.job_id, t.page) for t in claimed] == [(a.id, 1), (b.id, 1), (a.id, 2), (b.id, 2)]


async def test_concurrent_claims_never_return_the_same_task(db):
    await enqueue(db, pages=10)
    results = await asyncio.gather(*(claim(db, f"w{i}") for i in range(25)))
    ids = [t.id for t in results if t]
    assert len(ids) == 10 and len(set(ids)) == 10
    assert results.count(None) == 15


async def test_empty_queue_returns_none(db):
    assert await claim(db) is None


async def test_job_completes_with_products_and_price_history(db):
    job = await enqueue(db, pages=2)
    t1, t2 = await claim(db), await claim(db)
    assert await complete(db, t1, page_result(3, prefix="a")) == "done"
    assert (await get_job(db, job.id)).status == "running"
    assert await complete(db, t2, page_result(2, prefix="b", route="direct -> oxylabs")) == "done"

    job = await get_job(db, job.id)
    assert (job.status, job.pages_done, job.products_found) == ("done", 2, 5)
    assert job.route == "direct; direct -> oxylabs" and job.finished_at
    async with db() as s:
        assert len((await s.execute(select(Product))).scalars().all()) == 5
        assert len((await s.execute(select(PriceHistory))).scalars().all()) == 5


async def test_empty_page_skips_remaining_pages(db):
    job = await enqueue(db, pages=5)
    t1, t2 = await claim(db), await claim(db)
    await complete(db, t2, page_result(0, cards=0))  # page 2 has no results
    assert [t.status for t in await tasks_of(db, job.id)] == ["running", "done", "skipped", "skipped", "skipped"]
    assert (await get_job(db, job.id)).last_page == 1
    assert await claim(db) is None  # nothing left to claim
    await complete(db, t1, page_result(3))
    assert (await get_job(db, job.id)).status == "done"


async def test_failed_page_is_retried_then_failed(db):
    job = await enqueue(db, pages=1)
    blocked = page_result(0, cards=0, errors=[{"page": 1, "route": "direct", "error": "blocked"}])
    assert await complete(db, await claim(db), blocked) == "queued"  # attempt 1 -> retry
    task = await claim(db)
    assert task.attempts == 2
    assert await complete(db, task, blocked) == "failed"
    job = await get_job(db, job.id)
    assert job.status == "failed" and json.loads(job.error)[0]["error"] == "blocked"


async def test_result_discarded_when_lease_was_lost(db):
    await enqueue(db, pages=1)
    task = await claim(db, W1)
    async with db() as s:  # lease expired and another worker took over
        await s.execute(update(ScrapeTask).where(ScrapeTask.id == task.id).values(worker_id=W2))
        await s.commit()
    assert await complete(db, task, page_result(3), worker=W1) == "lost"
    async with db() as s:
        assert (await s.execute(select(Product))).scalars().all() == []


async def test_reaper_requeues_dead_workers_tasks_then_fails_them(db):
    job = await enqueue(db, pages=1)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)

    async def expire():
        async with db() as s:
            await s.execute(update(ScrapeTask).values(heartbeat_at=stale))
            await s.commit()

    await claim(db)
    await expire()
    async with db() as s:
        assert await q.reap_stale_tasks(s, lease_seconds=60, max_attempts=2) == 1
    assert (await tasks_of(db, job.id))[0].status == "queued"

    await claim(db)  # attempt 2 also dies
    await expire()
    async with db() as s:
        await q.reap_stale_tasks(s, lease_seconds=60, max_attempts=2)
    assert (await tasks_of(db, job.id))[0].status == "failed"
    assert (await get_job(db, job.id)).status == "failed"


async def test_reaper_ignores_fresh_tasks(db):
    await enqueue(db, pages=1)
    await claim(db)
    async with db() as s:
        assert await q.reap_stale_tasks(s, lease_seconds=60, max_attempts=2) == 0


async def test_release_task_puts_it_back(db):
    job = await enqueue(db, pages=1)
    task = await claim(db)
    async with db() as s:
        await q.release_task(s, task, W1, "shutdown")
    t = (await tasks_of(db, job.id))[0]
    assert t.status == "queued" and t.worker_id is None


async def test_cancel_skips_queued_pages_and_running_page_finishes(db):
    job = await enqueue(db, pages=3)
    running = await claim(db)
    async with db() as s:
        cancelled = await q.cancel_job(s, job.id)
    assert cancelled.status == "cancelled"
    assert [t.status for t in await tasks_of(db, job.id)] == ["running", "skipped", "skipped"]
    await complete(db, running, page_result(2))
    job = await get_job(db, job.id)
    assert job.status == "cancelled" and job.products_found == 2 and job.finished_at


async def test_upsert_keeps_known_fields_and_records_price_changes(db):
    from app.storage import save_products

    base = {"sku": "1", "name": "TV", "price": 500.0, "regular_price": 600.0, "model": "M1", "rating": 4.5}
    async with db() as s:
        await save_products(s, [base], "tv")
        # Same price again: no new history row. Degraded data (no model/rating): keep old values.
        await save_products(s, [{**base, "model": None, "rating": None}], "tv")
        await save_products(s, [{**base, "price": 450.0}], "tv")  # price drop
        await s.commit()
        product = (await s.execute(select(Product))).scalar_one()
        history = (await s.execute(select(PriceHistory).order_by(PriceHistory.id))).scalars().all()
    assert (product.price, product.model, product.rating) == (450.0, "M1", 4.5)
    assert [h.price for h in history] == [500.0, 450.0]


async def test_direct_cooldown_is_shared_and_cleared(db):
    fell_back = {"fell_back": True, "proxy_failed": False, "route": "direct -> oxylabs",
                 "products": [1], "errors": [{"error": "blocked"}]}
    async with db() as s:
        await q.update_direct_cooldown(s, fell_back, started_with_proxy=False, retry_after=600)
        assert 590 <= await q.direct_cooldown_remaining(s) <= 600
        assert await s.scalar(select(RouteState.reason)) == "blocked"

        # A proxy failure must not start a cooldown.
        await q.update_direct_cooldown(s, {**fell_back, "proxy_failed": True}, False, 600)
        assert await q.direct_cooldown_remaining(s) > 0

        clean = {"fell_back": False, "route": "direct", "products": [1], "errors": []}
        await q.update_direct_cooldown(s, clean, started_with_proxy=False, retry_after=600)
        assert await q.direct_cooldown_remaining(s) == 0
