"""Async database session and engine with pgvector support."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings

# Serverless functions are frozen between requests; pooled connections would go stale.
SERVERLESS = bool(os.environ.get("VERCEL"))


def async_database_url(url: str) -> tuple[URL, dict[str, Any]]:
    """Accept plain postgres:// URLs (as Neon and most hosts provide) for asyncpg.

    asyncpg doesn't understand libpq's ?sslmode=..., so it becomes connect_args["ssl"].
    """
    u = make_url(url)
    if u.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
        u = u.set(drivername="postgresql+asyncpg")
    query = dict(u.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    connect_args: dict[str, Any] = {"ssl": "require"} if sslmode and sslmode != "disable" else {}
    if "-pooler" in (u.host or ""):
        # PgBouncer in transaction mode can't keep asyncpg's per-connection prepared statements.
        connect_args["statement_cache_size"] = 0
        query["prepared_statement_cache_size"] = "0"
    return u.set(query=query), connect_args


def sync_database_url(url: str) -> URL:
    """psycopg2 URL for the worker; libpq query params (sslmode) pass through unchanged."""
    u = make_url(url)
    return u.set(drivername="postgresql+psycopg2") if u.drivername.startswith(("postgres", "postgresql")) else u


_url, _connect_args = async_database_url(settings.database_url)
engine = create_async_engine(
    _url,
    echo=settings.debug,
    pool_pre_ping=True,
    connect_args=_connect_args,
    **({"poolclass": NullPool} if SERVERLESS else {"pool_size": 5, "max_overflow": 10}),
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database session (e.g. in workers)."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
