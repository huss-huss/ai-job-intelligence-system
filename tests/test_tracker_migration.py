"""Migration 003_tracker: upgrade, `alembic check` for the tracker tables, downgrade and re-upgrade.

Runs on SQLite always. With TEST_DATABASE_URL it also runs on Postgres, inside a throwaway
schema (dropped afterwards). If the server has no pgvector, migrations 001-002 can't run, so the
older tables are created from the models and stamped at 002 before upgrading.
"""

import os

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from tests.sqlite_support import alembic_config, sqlite_engine

TRACKER_TABLES = {"applications", "talent_networks", "app_settings"}
USER_TABLES = {"users", "invites", "auth_throttle", "user_job_scores"}  # 004_multi_user
OWN_TABLES = TRACKER_TABLES | USER_TABLES
SCHEMA = "firstfetch_migration_test"


def only_tracker_tables(obj, name, type_, reflected, compare_to):
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return (table.name if table is not None else name) in OWN_TABLES


def _check(conn):
    cfg = alembic_config(conn)
    cfg.attributes["include_object"] = only_tracker_tables
    command.check(cfg)  # raises if the models and the migration disagree


def test_single_head_and_tracker_revision_follows_002():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("003_tracker").down_revision == "002"


def test_sqlite_upgrade_check_downgrade(tmp_path):
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    assert TRACKER_TABLES <= set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        _check(conn)
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "002")
    assert not OWN_TABLES & set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        _check(conn)
    columns = {c["name"] for c in inspect(engine).get_columns("applications")}
    assert {"job_id", "status", "notes", "notified_at", "applied_at", "status_changed_at"} <= columns


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_upgrade_check_downgrade():
    from app.database import Base, sync_database_url

    engine = create_engine(sync_database_url(TEST_DATABASE_URL))
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        has_vector = conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).first()
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
            if has_vector:
                command.upgrade(alembic_config(conn), "head")
            else:
                older = [t for t in Base.metadata.sorted_tables if t.name not in OWN_TABLES | {"embeddings"}]
                Base.metadata.create_all(conn, tables=older)
                command.stamp(alembic_config(conn), "002")
                command.upgrade(alembic_config(conn), "head")
            _check(conn)
            names = set(inspect(conn).get_table_names())
            assert TRACKER_TABLES <= names
            assert ("embeddings" in names) == bool(has_vector)
            fks = {fk["referred_table"]: fk for fk in inspect(conn).get_foreign_keys("applications")}
            assert set(fks) == {"jobs_raw", "users"}
            assert all(fk["options"].get("ondelete") == "CASCADE" for fk in fks.values())
            command.downgrade(alembic_config(conn), "002")
            assert not OWN_TABLES & set(inspect(conn).get_table_names())
            command.upgrade(alembic_config(conn), "head")
            _check(conn)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
