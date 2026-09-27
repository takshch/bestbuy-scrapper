import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401 - registers tables on Base.metadata
from app.config import get_settings
from app.db import Base, _asyncpg_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
# Serializes migrations when several API replicas start at once.
MIGRATION_LOCK_ID = 727_274_001


def _do_run_migrations(connection) -> None:
    connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})


async def run_async_migrations() -> None:
    url, connect_args = _asyncpg_url(get_settings().database_url)
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    url, _ = _asyncpg_url(get_settings().database_url)
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
