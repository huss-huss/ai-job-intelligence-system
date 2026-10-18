"""Synchronous database session for the worker (RQ runs in sync context)."""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import SERVERLESS, sync_database_url

sync_engine = create_engine(
    sync_database_url(settings.database_url),
    echo=settings.debug,
    pool_pre_ping=True,
    **({"poolclass": NullPool} if SERVERLESS else {"pool_size": 2, "max_overflow": 5}),
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
