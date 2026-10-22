"""Migration 006_web_sessions: the sessions table on top of 005_llm_budget; downgrade drops only it.
SQLite always, Postgres with TEST_DATABASE_URL (throwaway schema)."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from tests.sqlite_support import alembic_config, sqlite_engine
from tests.test_tracker_migration import OWN_TABLES, _check

SCHEMA = "firstfetch_web_sessions_test"
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def test_006_follows_005_on_a_single_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("006_web_sessions").down_revision == "005_llm_budget"


def _seed_005(conn) -> int:
    conn.execute(text("INSERT INTO users (telegram_chat_id, role, paused) VALUES ('777', 'member', false)"))
    return conn.execute(text("SELECT id FROM users WHERE telegram_chat_id = '777'")).scalar()


def _use_006(conn, uid: int) -> None:
    cols = {c["name"] for c in inspect(conn).get_columns("sessions")}
    assert {"token_hash", "user_id", "created_at", "expires_at", "last_seen", "user_agent_hash"} <= cols
    (fk,) = inspect(conn).get_foreign_keys("sessions")
    assert fk["referred_table"] == "users" and fk["options"].get("ondelete") == "CASCADE"
    insert = text("INSERT INTO sessions (token_hash, user_id, expires_at, last_seen) VALUES (:h, :u, :e, :n)")
    conn.execute(insert, {"h": "a" * 64, "u": uid, "e": NOW + timedelta(days=30), "n": NOW})
    with pytest.raises(Exception):  # noqa: B017 - unique token_hash, the error type differs per driver
        with conn.begin_nested():
            conn.execute(insert, {"h": "a" * 64, "u": uid, "e": NOW, "n": NOW})
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 0  # cascaded


def test_sqlite_upgrade_check_downgrade(tmp_path):
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "005_llm_budget")
        uid = _seed_005(conn)
        assert "sessions" not in inspect(conn).get_table_names()
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "006_web_sessions")
        assert "sessions" in inspect(conn).get_table_names()
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "005_llm_budget")
        assert "sessions" not in inspect(conn).get_table_names()
        assert conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": uid}).scalar() == 1
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        _check(conn)  # the models match at head (007 adds login_links on top)
        _use_006(conn, uid)
    engine.dispose()


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_upgrade_check_downgrade():
    from app.database import Base, sync_database_url

    engine = create_engine(sync_database_url(TEST_DATABASE_URL))
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        has_vector = conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).first()
    scoped = create_engine(
        sync_database_url(TEST_DATABASE_URL), connect_args={"options": f"-csearch_path={SCHEMA},public"}
    )
    try:
        with scoped.begin() as conn:
            if has_vector:
                command.upgrade(alembic_config(conn), "005_llm_budget")
            else:
                older = [t for t in Base.metadata.sorted_tables if t.name not in OWN_TABLES | {"embeddings"}]
                Base.metadata.create_all(conn, tables=older)
                command.stamp(alembic_config(conn), "002")
                command.upgrade(alembic_config(conn), "005_llm_budget")
            uid = _seed_005(conn)
            command.upgrade(alembic_config(conn), "head")
            _check(conn)
            command.downgrade(alembic_config(conn), "005_llm_budget")
            assert "sessions" not in inspect(conn).get_table_names()
            assert conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": uid}).scalar() == 1
            command.upgrade(alembic_config(conn), "head")
            _use_006(conn, uid)
    finally:
        scoped.dispose()
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
