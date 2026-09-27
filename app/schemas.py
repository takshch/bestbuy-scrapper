from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=255, examples=["laptop"])
    max_pages: int = Field(1, ge=1, le=50)


class ScrapeJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    query: str
    max_pages: int
    status: str
    route: Optional[str]
    products_found: int
    pages_done: int
    last_page: Optional[int]
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class ScrapeTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    page: int
    status: str
    attempts: int
    worker_id: Optional[str]
    route: Optional[str]
    fell_back: bool
    products_found: int
    errors: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    model: Optional[str]
    url: Optional[str]
    image_url: Optional[str]
    price: Optional[float]
    regular_price: Optional[float]
    rating: Optional[float]
    review_count: Optional[int]
    availability: Optional[str]
    search_query: Optional[str]
    first_seen_at: datetime
    updated_at: datetime


class ProductPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[ProductOut]


class PricePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price: Optional[float]
    regular_price: Optional[float]
    job_id: Optional[int]
    observed_at: datetime


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    hostname: str
    concurrency: int
    busy: int
    tasks_done: int
    tasks_failed: int
    started_at: datetime
    heartbeat_at: datetime
    alive: bool = False


class RouteStats(BaseModel):
    pages: int
    succeeded: int
    success_rate: Optional[float]


class Stats(BaseModel):
    jobs: Dict[str, int]
    tasks: Dict[str, int]
    queue_depth: int
    products: int
    price_changes_24h: int
    pages_last_hour: int
    products_last_hour: int
    # Last 24h of finished page tasks, by how they were fetched.
    routes_24h: Dict[str, RouteStats]
    direct_cooldown_seconds: int
    workers: List[WorkerOut]
