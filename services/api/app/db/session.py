"""Database engine and session management.

The request path is fully asynchronous (asyncpg); Alembic and one-off management
scripts use a synchronous engine (psycopg 3) because migrations are easier to
reason about serially. Both read the same DSN from :mod:`app.core.config`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

#: Async engine used by API requests.
async_engine: AsyncEngine = create_async_engine(
    _settings.sqlalchemy_async_dsn,
    echo=_settings.DB_ECHO,
    pool_size=_settings.DB_POOL_SIZE,
    max_overflow=_settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

_sync_engine: Optional[Engine] = None
SyncSessionLocal: Optional[sessionmaker] = None


def get_sync_engine() -> Engine:
    """Lazily built synchronous engine for Alembic and CLI scripts."""

    global _sync_engine, SyncSessionLocal
    if _sync_engine is None:
        _sync_engine = create_engine(
            _settings.sqlalchemy_dsn,
            echo=_settings.DB_ECHO,
            pool_pre_ping=True,
            future=True,
        )
        SyncSessionLocal = sessionmaker(bind=_sync_engine, expire_on_commit=False)
    return _sync_engine


def get_sync_session() -> Session:
    """Open a synchronous session (scripts only, never the request path)."""

    get_sync_engine()
    assert SyncSessionLocal is not None  # set by get_sync_engine
    return SyncSessionLocal()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a transactional session.

    Commit is explicit in the route or service layer. On any exception the
    transaction is rolled back before the session is returned to the pool, so a
    failed request can never leave a half-applied risk recalculation behind.
    """

    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_database_health() -> dict[str, object]:
    """Report connectivity plus the PostGIS version.

    Used by ``GET /health/ready``. PostGIS is reported explicitly because the
    platform is unusable without it, and a plain ``postgres`` image starting up
    by mistake is an easy misconfiguration to miss.
    """

    result: dict[str, object] = {"connected": False, "postgis": None}
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))
        result["connected"] = True
        try:
            version = await session.scalar(text("SELECT PostGIS_Version()"))
            result["postgis"] = version
        except Exception:  # noqa: BLE001 - extension genuinely absent
            result["postgis"] = None
    return result

