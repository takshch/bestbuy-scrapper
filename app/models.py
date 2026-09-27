from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String(128))
    url: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Optional[float]] = mapped_column(Float)
    regular_price: Mapped[Optional[float]] = mapped_column(Float)
    rating: Mapped[Optional[float]] = mapped_column(Float)
    review_count: Mapped[Optional[int]] = mapped_column(Integer)
    availability: Mapped[Optional[str]] = mapped_column(String(64))
    search_query: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PriceHistory(Base):
    """One row per observed price change (first sighting included)."""

    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_sku_observed", "sku", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32))
    price: Mapped[Optional[float]] = mapped_column(Float)
    regular_price: Mapped[Optional[float]] = mapped_column(Float)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="SET NULL"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScrapeJob(Base):
    """A search request. Split into one ScrapeTask per results page."""

    __tablename__ = "scrape_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(255))
    max_pages: Mapped[int] = mapped_column(Integer)
    # pending | running | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # Routes the job's pages used, e.g. "direct, direct -> oxylabs/residential/US ..."
    route: Mapped[Optional[str]] = mapped_column(Text)
    products_found: Mapped[int] = mapped_column(Integer, default=0)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    # Set when a page comes back empty: pages after it are skipped.
    last_page: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ScrapeTask(Base):
    """One results page of a job; the unit of work claimed by workers."""

    __tablename__ = "scrape_tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "page"),
        # Claim query: oldest queued task first.
        Index("ix_scrape_tasks_queued", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scrape_jobs.id", ondelete="CASCADE"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    # queued | running | done | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    route: Mapped[Optional[str]] = mapped_column(String(255))
    fell_back: Mapped[bool] = mapped_column(default=False)
    products_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[Optional[str]] = mapped_column(Text)  # JSON list
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Worker(Base):
    """Registry of worker processes, for monitoring."""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    concurrency: Mapped[int] = mapped_column(Integer)
    busy: Mapped[int] = mapped_column(Integer, default=0)
    tasks_done: Mapped[int] = mapped_column(Integer, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class RouteState(Base):
    """Shared routing state across workers (e.g. direct IP cooldown)."""

    __tablename__ = "route_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    blocked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
