"""Async engine + sessionmaker. Parametry puli jak postgres-js w oryginale
(max 10 polaczen, graceful close)."""

from collections.abc import AsyncIterator
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def build_dsn() -> str:
    if settings.DATABASE_URL:
        url = settings.DATABASE_URL
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
        elif url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url.removeprefix("postgres://")
        return url
    auth = quote(settings.DB_USER, safe="")
    if settings.DB_PASS:
        auth += ":" + quote(settings.DB_PASS, safe="")
    return f"postgresql+asyncpg://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"


engine = create_async_engine(build_dsn(), pool_size=10, max_overflow=0)

async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
