"""Migration 007_login_links: the login_links table on top of 006_web_sessions; downgrade drops only it.
SQLite (the Postgres path is covered by test_tracker_migration's round trip)."""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from tests.sqlite_support import alembic_config, sqlite_engine
from tests.test_tracker_migration import _check

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def test_007_follows_006_on_a_single_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("007_login_links").down_revision == "006_web_sessions"


def test_sqlite_upgrade_check_downgrade(tmp_path):
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "006_web_sessions")
        conn.execute(text("INSERT INTO users (telegram_chat_id, role, paused) VALUES ('777', 'member', false)"))
        uid = conn.execute(text("SELECT id FROM users WHERE telegram_chat_id = '777'")).scalar()
        assert "login_links" not in inspect(conn).get_table_names()
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "007_login_links")
        (fk,) = inspect(conn).get_foreign_keys("login_links")
        assert fk["referred_table"] == "users" and fk["options"].get("ondelete") == "CASCADE"
        insert = text("INSERT INTO login_links (token_hash, user_id, expires_at) VALUES (:h, :u, :e)")
        conn.execute(insert, {"h": "a" * 64, "u": uid, "e": NOW + timedelta(minutes=10)})
        with pytest.raises(Exception):  # noqa: B017 - unique token_hash, the error type differs per driver
            with conn.begin_nested():
                conn.execute(insert, {"h": "a" * 64, "u": uid, "e": NOW})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        assert conn.execute(text("SELECT count(*) FROM login_links")).scalar() == 0  # cascaded
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "006_web_sessions")
        assert "login_links" not in inspect(conn).get_table_names()
        assert "sessions" in inspect(conn).get_table_names()
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        _check(conn)  # the models match the migrations at head
    engine.dispose()
