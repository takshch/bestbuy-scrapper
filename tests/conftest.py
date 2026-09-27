"""Shared fixtures.

- Unit tests run anywhere.
- ``db`` tests need a throwaway Postgres database whose name contains "test":
      TEST_DATABASE_URL=postgresql://scraper:scraper@localhost:5432/bestbuy_test pytest
  (e.g. `docker compose up -d postgres` then `createdb bestbuy_test`). The schema is
  dropped and rebuilt from the Alembic migrations once per run.
- ``browser`` tests launch CloakBrowser: RUN_BROWSER_TESTS=1 pytest -m browser
"""
import asyncio
import os
from urllib.parse import urlsplit

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
# app.db builds its engine at import time, so point it at the test DB first.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL or "postgresql://unused:unused@localhost:1/unused"
for var in ("OXYLABS_USERNAME", "OXYLABS_PASSWORD", "OXYLABS_STATE"):
    os.environ.pop(var, None)

from app.config import Settings, get_settings  # noqa: E402

# Never read the developer's .env (real proxy credentials etc.) during tests.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

TABLES = "products, price_history, scrape_tasks, scrape_jobs, workers, route_state"


def pytest_collection_modifyitems(config, items):
    skip_db = pytest.mark.skip(reason="set TEST_DATABASE_URL to run database tests")
    skip_browser = pytest.mark.skip(reason="set RUN_BROWSER_TESTS=1 to run browser tests")
    for item in items:
        if "db" in item.keywords and not TEST_DATABASE_URL:
            item.add_marker(skip_db)
        if "browser" in item.keywords and os.environ.get("RUN_BROWSER_TESTS") != "1":
            item.add_marker(skip_browser)


def make_settings(**overrides) -> Settings:
    """Settings isolated from the developer's .env file."""
    base = dict(database_url=os.environ["DATABASE_URL"], request_jitter_min=0, request_jitter_max=0)
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture(scope="session")
def migrated_db():
    """Rebuild the test schema from migrations (once per test session)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    dbname = urlsplit(TEST_DATABASE_URL).path.lstrip("/")
    assert "test" in dbname, f"refusing to wipe non-test database {dbname!r}"

    import asyncpg

    async def reset():
        conn = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(reset())
    from app.migrate import run_migrations

    run_migrations()
    yield


@pytest.fixture
async def db(migrated_db):
    """Clean tables before each db test; yields a session factory."""
    from sqlalchemy import text

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await s.commit()
    yield SessionLocal
