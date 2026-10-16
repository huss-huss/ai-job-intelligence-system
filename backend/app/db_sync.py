"""Synchronous database session for the worker (RQ runs in sync context)."""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base

# Sync URL (psycopg2)
_sync_url = settings.database_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")

sync_engine = create_engine(
    _sync_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=5,
)

SyncSessionLocal = sessionmaker(sync_engine, autocommit=False, autoflush=False)


@contextmanager
def get_sync_session() -> Generator[Session, None, None]:
    """Yield a sync session for use in worker tasks."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
