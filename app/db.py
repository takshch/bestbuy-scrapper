from typing import AsyncIterator, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


def _asyncpg_url(raw: str) -> "tuple[str, Dict[str, str]]":
    """Turn a `postgresql://` URL into an asyncpg URL.

    asyncpg doesn't accept libpq's `sslmode` query param, so it's moved to connect_args.
    """
    parts = urlsplit(raw)
    params = dict(parse_qsl(parts.query))
    connect_args = {}
    sslmode = params.pop("sslmode", None)
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = sslmode
    query = urlencode(params)
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, query, parts.fragment)), connect_args


_url, _connect_args = _asyncpg_url(get_settings().database_url)
engine = create_async_engine(
    _url,
    connect_args=_connect_args,
    pool_pre_ping=True,  # survive the Postgres container restarting
    pool_size=5,
    max_overflow=5,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
