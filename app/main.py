import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.migrate import run_migrations
from app.queue import direct_cooldown_remaining
from app.routers import products, scrape, stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic runs its own event loop, so apply migrations in a thread.
    await asyncio.to_thread(run_migrations)
    yield
    await engine.dispose()


app = FastAPI(
    title="Best Buy Scraper API",
    version="2.0.0",
    description="Queue Best Buy searches for distributed scrape workers and query the results.",
    lifespan=lifespan,
)
app.include_router(scrape.router)
app.include_router(products.router)
app.include_router(stats.router)


@app.get("/health", tags=["health"])
async def health():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    async with SessionLocal() as session:
        cooldown = await direct_cooldown_remaining(session)
    s = get_settings()
    return {
        "status": "ok",
        "database": "ok",
        "proxy_mode": s.proxy_mode,
        "proxy": f"oxylabs/{s.oxylabs_proxy_type}/{s.oxylabs_country}" if s.proxy_configured else None,
        "direct_cooldown_seconds": cooldown,
    }
