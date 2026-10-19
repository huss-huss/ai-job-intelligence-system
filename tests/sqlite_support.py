"""Run the Postgres-typed models on SQLite in tests.

JSONB and ARRAY have no SQLite DDL; both are stored as JSON text there. Only tests import this.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from app.models import JobAnalysis
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles

BACKEND = Path(__file__).resolve().parent.parent / "backend"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_sqlite(type_, compiler, **kw):  # noqa: ARG001
    return "JSON"


# Binding a Python list needs a JSON type on SQLite; Postgres keeps the real ARRAY.
_skills = JobAnalysis.__table__.c.skills
_skills.type = _skills.type.with_variant(JSON(), "sqlite")


def sqlite_engine(path: str | Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # SQLite ignores ON DELETE CASCADE unless asked
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def alembic_config(connection=None) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg
