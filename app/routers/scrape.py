from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import ScrapeJob, ScrapeTask
from app.queue import cancel_job, enqueue_job
from app.schemas import ScrapeJobOut, ScrapeRequest, ScrapeTaskOut

router = APIRouter(prefix="/scrape", tags=["scrape"])


@router.post("", response_model=ScrapeJobOut, status_code=status.HTTP_202_ACCEPTED)
async def start_scrape(body: ScrapeRequest, session: AsyncSession = Depends(get_session)):
    """Queue a Best Buy search scrape (one task per page, processed by workers in parallel).
    Poll GET /scrape/jobs/{id} for progress."""
    settings = get_settings()
    if settings.proxy_mode == "always" and not settings.proxy_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "PROXY_MODE=always but Oxylabs is not configured (set OXYLABS_USERNAME / OXYLABS_PASSWORD).",
        )
    return await enqueue_job(session, body.query, body.max_pages)


@router.get("/jobs", response_model=List[ScrapeJobOut])
async def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(ScrapeJob).order_by(ScrapeJob.id.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(ScrapeJob.status == status_filter)
    return (await session.execute(stmt)).scalars().all()


@router.get("/jobs/{job_id}", response_model=ScrapeJobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    job = await session.get(ScrapeJob, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.get("/jobs/{job_id}/tasks", response_model=List[ScrapeTaskOut])
async def get_job_tasks(job_id: int, session: AsyncSession = Depends(get_session)):
    """Per-page progress: status, attempts, worker, route and errors."""
    if not await session.get(ScrapeJob, job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    stmt = select(ScrapeTask).where(ScrapeTask.job_id == job_id).order_by(ScrapeTask.page)
    return (await session.execute(stmt)).scalars().all()


@router.post("/jobs/{job_id}/cancel", response_model=ScrapeJobOut)
async def cancel(job_id: int, session: AsyncSession = Depends(get_session)):
    """Skip the job's queued pages. Pages already being scraped finish normally."""
    job = await cancel_job(session, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job
