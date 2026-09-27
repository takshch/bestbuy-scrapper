from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import PriceHistory, Product, ScrapeJob, ScrapeTask, Worker
from app.queue import direct_cooldown_remaining
from app.schemas import RouteStats, Stats, WorkerOut

router = APIRouter(tags=["stats"])


def _route_bucket():
    """Classify how a page was fetched from its route string."""
    return case(
        (ScrapeTask.route.is_(None), "none"),
        (ScrapeTask.route.like("%oxylabs (unavailable)%"), "proxy_unavailable"),
        (ScrapeTask.route.like("direct -> %"), "direct_then_proxy"),
        (ScrapeTask.route.like("oxylabs%"), "proxy"),
        else_="direct",
    )


@router.get("/stats", response_model=Stats)
async def stats(session: AsyncSession = Depends(get_session)):
    """Queue, throughput, per-route success rates and live workers."""
    now = datetime.now(timezone.utc)
    hour_ago, day_ago = now - timedelta(hours=1), now - timedelta(days=1)

    jobs = dict((await session.execute(select(ScrapeJob.status, func.count()).group_by(ScrapeJob.status))).all())
    tasks = dict((await session.execute(select(ScrapeTask.status, func.count()).group_by(ScrapeTask.status))).all())

    pages_hour, products_hour = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(ScrapeTask.products_found), 0)).where(
                ScrapeTask.status == "done", ScrapeTask.finished_at >= hour_ago
            )
        )
    ).one()

    bucket = _route_bucket().label("bucket")
    route_rows = (
        await session.execute(
            select(bucket, func.count(), func.sum(case((ScrapeTask.status == "done", 1), else_=0)))
            .where(ScrapeTask.finished_at >= day_ago, ScrapeTask.status.in_(("done", "failed")))
            .group_by(bucket)
        )
    ).all()
    routes = {
        name: RouteStats(pages=n, succeeded=ok or 0, success_rate=round((ok or 0) / n, 3) if n else None)
        for name, n, ok in route_rows
    }

    lease = timedelta(seconds=get_settings().task_lease_seconds)
    workers = []
    for w in (await session.execute(
        select(Worker).where(Worker.stopped_at.is_(None)).order_by(Worker.started_at)
    )).scalars():
        out = WorkerOut.model_validate(w)
        out.alive = w.heartbeat_at >= now - lease
        workers.append(out)

    return Stats(
        jobs=jobs,
        tasks=tasks,
        queue_depth=tasks.get("queued", 0),
        products=await session.scalar(select(func.count()).select_from(Product)) or 0,
        price_changes_24h=await session.scalar(
            select(func.count()).select_from(PriceHistory).where(PriceHistory.observed_at >= day_ago)
        ) or 0,
        pages_last_hour=pages_hour,
        products_last_hour=products_hour,
        routes_24h=routes,
        direct_cooldown_seconds=await direct_cooldown_remaining(session),
        workers=workers,
    )
