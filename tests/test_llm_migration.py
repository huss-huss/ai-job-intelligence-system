"""Migration 005_llm_budget: the llm_usage ledger and users.resume_text; downgrade keeps every other row.
SQLite always, Postgres with TEST_DATABASE_URL (throwaway schema, which also exercises the advisory lock)."""

import os
from datetime import UTC, date, datetime

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from tests.sqlite_support import alembic_config, sqlite_engine
from tests.test_tracker_migration import OWN_TABLES, _check

SCHEMA = "firstfetch_llm_budget_test"


def test_005_follows_004_on_a_single_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision("005_llm_budget").down_revision == "004_multi_user"


def _seed_004(conn):
    conn.execute(text("INSERT INTO users (telegram_chat_id, role, paused) VALUES ('555', 'member', false)"))
    uid = conn.execute(text("SELECT id FROM users WHERE telegram_chat_id = '555'")).scalar()
    conn.execute(text("INSERT INTO sources (id, name, base_url, enabled) VALUES (9, 's', 'https://e.com', true)"))
    conn.execute(text("INSERT INTO jobs_raw (id, source_id, url, title, content_hash) VALUES (9, 9, 'u', 't', 'h')"))
    conn.execute(
        text("INSERT INTO user_job_scores (user_id, job_id, score, match) VALUES (:u, 9, 50, '{}')"), {"u": uid}
    )
    return uid


def _use_005(conn, uid):
    conn.execute(text("UPDATE users SET resume_text = 'my resume' WHERE id = :u"), {"u": uid})
    conn.execute(
        text(
            "INSERT INTO llm_usage (day, user_id, feature, model, input_tokens, output_tokens, reasoning_tokens,"
            " cost_usd, status) VALUES (:d, :u, 'cover_letter', 'gpt-6-luna', 1500, 400, 150, 0.00035, 'recorded')"
        ),
        {"d": date(2026, 10, 19), "u": uid},
    )
    cols = {c["name"]: c for c in inspect(conn).get_columns("llm_usage")}
    expected = {"day", "user_id", "feature", "model", "input_tokens", "output_tokens", "reasoning_tokens", "cost_usd"}
    assert expected <= set(cols)
    fk = inspect(conn).get_foreign_keys("llm_usage")[0]
    assert fk["referred_table"] == "users" and fk["options"].get("ondelete") == "SET NULL"
    conn.execute(text("DELETE FROM user_job_scores WHERE user_id = :u"), {"u": uid})
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    row = conn.execute(text("SELECT user_id, cost_usd FROM llm_usage WHERE feature = 'cover_letter'")).one()
    assert tuple(row) == (None, 0.00035)


def test_sqlite_upgrade_downgrade_keeps_other_rows(tmp_path):
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "004_multi_user")
        uid = _seed_004(conn)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "005_llm_budget")
        assert {"resume_text", "resume_updated_at"} <= {c["name"] for c in inspect(conn).get_columns("users")}
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "004_multi_user")
        assert "llm_usage" not in inspect(conn).get_table_names()
        assert "resume_text" not in {c["name"] for c in inspect(conn).get_columns("users")}
        # Not rebuilt in batch mode, so SQLite's DROP TABLE didn't cascade into the user's rows.
        assert conn.execute(text("SELECT count(*) FROM user_job_scores")).scalar() == 1
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        _check(conn)  # `alembic check` compares the models with head only
        _use_005(conn, uid)


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_upgrade_check_downgrade_and_advisory_locked_reservation():
    from app.database import Base, sync_database_url

    from worker.budget import BudgetExceeded, BudgetGuard, SqlUsageStore

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
                command.upgrade(alembic_config(conn), "004_multi_user")
            else:
                older = [t for t in Base.metadata.sorted_tables if t.name not in OWN_TABLES | {"embeddings"}]
                Base.metadata.create_all(conn, tables=older)
                command.stamp(alembic_config(conn), "002")
                command.upgrade(alembic_config(conn), "004_multi_user")
            uid = _seed_004(conn)
            command.upgrade(alembic_config(conn), "head")
            _check(conn)
            command.downgrade(alembic_config(conn), "004_multi_user")
            assert "llm_usage" not in inspect(conn).get_table_names()
            assert conn.execute(text("SELECT count(*) FROM user_job_scores")).scalar() == 1
            command.upgrade(alembic_config(conn), "head")
        guard = BudgetGuard(
            SqlUsageStore(lambda: Session(scoped)),
            monthly_budget_usd=0.001,
            clock=lambda: datetime(2026, 10, 19, tzinfo=UTC),
        )
        guard.reserve("extract", "gpt-6-luna", 0.0006)
        with pytest.raises(BudgetExceeded):
            guard.reserve("extract", "gpt-6-luna", 0.0006)
        assert guard.summary().month_cost == pytest.approx(0.0006)
        with scoped.begin() as conn:
            _use_005(conn, uid)
    finally:
        scoped.dispose()
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
