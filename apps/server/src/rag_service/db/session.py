"""Async SQLAlchemy engine + session factory.

Engine and session maker are constructed lazily so importing this module
does not require environment variables (e.g. DATABASE_URL) to be set.
"""

from __future__ import annotations

from functools import lru_cache
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rag_service import config as _config


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return a process-wide async engine, created on first call."""
    settings = _config.settings  # resolved lazily by config.__getattr__
    return create_async_engine(
        settings.database_url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1200,
    )


@lru_cache(maxsize=1)
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return a process-wide async session factory, created on first call."""
    return async_sessionmaker(
        get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session, commit on success, rollback on error."""
    sm = get_session_maker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def __getattr__(name: str):
    """Backward-compat: expose ``async_session_maker`` lazily.

    Accessing ``session.async_session_maker`` returns the same object as
    ``get_session_maker()`` without instantiating it at import time.
    """
    if name == "async_session_maker":
        return get_session_maker()
    if name == "engine":
        return get_engine()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
