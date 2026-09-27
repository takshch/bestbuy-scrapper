"""HTTP API tests (FastAPI in-process, real Postgres)."""
import httpx
import pytest

from app.config import get_settings
from app.main import app
from app.queue import claim_task, complete_task

pytestmark = pytest.mark.db


@pytest.fixture
async def client(db):
    transport = httpx.ASGITransport(app=app)  # lifespan not run: the db fixture already migrated
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def run_job(db, pages, products_per_page=2, prices=None):
    """Play the worker: claim and complete every queued task."""
    for n in range(pages):
        async with db() as s:
            task = await claim_task(s, "w")
        price = (prices or [100.0])[n % len(prices or [100.0])]
        result = {"route": "direct", "cards": products_per_page, "errors": [],
                  "products": [{"sku": f"{task.page}{i}", "name": f"Item {task.page}-{i}", "price": price + i,
                                "regular_price": price + 50, "rating": 4.0 + i / 10} for i in range(products_per_page)]}
        async with db() as s:
            await complete_task(s, task, "w", result, 2)


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"


async def test_start_scrape_queues_page_tasks(client):
    r = await client.post("/scrape", json={"query": "laptop", "max_pages": 3})
    assert r.status_code == 202
    job = r.json()
    assert job["status"] == "pending" and job["pages_done"] == 0

    tasks = (await client.get(f"/scrape/jobs/{job['id']}/tasks")).json()
    assert [(t["page"], t["status"]) for t in tasks] == [(1, "queued"), (2, "queued"), (3, "queued")]


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "tv", "max_pages": 0}, {"query": "tv", "max_pages": 51}])
async def test_start_scrape_validates_input(client, body):
    assert (await client.post("/scrape", json=body)).status_code == 422


async def test_always_mode_without_credentials_is_rejected(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "proxy_mode", "always")
    assert (await client.post("/scrape", json={"query": "tv"})).status_code == 503


async def test_job_lifecycle_and_products(client, db):
    job = (await client.post("/scrape", json={"query": "tv", "max_pages": 2})).json()
    await run_job(db, 2, prices=[300.0, 100.0])

    job = (await client.get(f"/scrape/jobs/{job['id']}")).json()
    assert job["status"] == "done" and job["products_found"] == 4 and job["pages_done"] == 2

    page = (await client.get("/products", params={"sort": "price_asc"})).json()
    assert page["total"] == 4
    assert [p["price"] for p in page["items"]] == [100.0, 101.0, 300.0, 301.0]

    filtered = (await client.get("/products", params={"min_price": 150, "q": "item 1"})).json()
    assert [p["sku"] for p in filtered["items"]] == ["10", "11"]

    assert (await client.get("/products", params={"search_query": "other"})).json()["total"] == 0
    assert (await client.get("/products/10")).json()["regular_price"] == 350.0
    assert (await client.get("/products/nope")).status_code == 404


async def test_price_history(client, db):
    await client.post("/scrape", json={"query": "tv", "max_pages": 1})
    await run_job(db, 1, products_per_page=1, prices=[500.0])
    await client.post("/scrape", json={"query": "tv", "max_pages": 1})
    await run_job(db, 1, products_per_page=1, prices=[450.0])

    history = (await client.get("/products/10/history")).json()
    assert [h["price"] for h in history] == [500.0, 450.0]
    assert (await client.get("/products/nope/history")).status_code == 404


async def test_cancel_job(client):
    job = (await client.post("/scrape", json={"query": "tv", "max_pages": 3})).json()
    r = await client.post(f"/scrape/jobs/{job['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    statuses = {t["status"] for t in (await client.get(f"/scrape/jobs/{job['id']}/tasks")).json()}
    assert statuses == {"skipped"}
    assert (await client.post("/scrape/jobs/999/cancel")).status_code == 404


async def test_list_jobs_filters_by_status(client):
    for q in ("a", "b"):
        await client.post("/scrape", json={"query": q})
    jobs = (await client.get("/scrape/jobs")).json()
    assert [j["query"] for j in jobs] == ["b", "a"]
    assert (await client.get("/scrape/jobs", params={"status": "done"})).json() == []


async def test_stats(client, db):
    await client.post("/scrape", json={"query": "tv", "max_pages": 3})
    await run_job(db, 2)
    stats = (await client.get("/stats")).json()
    assert stats["tasks"] == {"done": 2, "queued": 1}
    assert stats["queue_depth"] == 1 and stats["products"] == 4
    assert stats["pages_last_hour"] == 2 and stats["products_last_hour"] == 4
    assert stats["routes_24h"]["direct"] == {"pages": 2, "succeeded": 2, "success_rate": 1.0}
    assert stats["jobs"] == {"running": 1}
