"""Migration 004_multi_user: the owner is created from TELEGRAM_CHAT_ID and the profile, existing
tracker / talent-network rows and the pause flag move to them, (user, job) is unique; downgrade keeps
the owner's rows. SQLite always, Postgres with TEST_DATABASE_URL (throwaway schema)."""

import json
import os

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.sqlite_support import alembic_config, sqlite_engine
from tests.test_tracker_migration import OWN_TABLES

SCHEMA = "firstfetch_multi_user_test"
PROFILE = "name: From env\ntarget_titles: [backend]\nskills: {python: 3}\n"


def _seed_003(conn):
    conn.execute(text("INSERT INTO sources (id, name, base_url, enabled) VALUES (1, 's', 'https://e.com', true)"))
    for i in (1, 2):
        conn.execute(
            text(
                "INSERT INTO jobs_raw (id, source_id, url, title, content_hash) "
                "VALUES (:i, 1, 'https://e.com/' || :i, 'Backend', 'h' || :i)"
            ),
            {"i": i},
        )
    conn.execute(text("INSERT INTO applications (job_id, status, notes) VALUES (1, 'applied', 'note')"))
    conn.execute(text("INSERT INTO applications (job_id, status) VALUES (2, 'not_interested')"))
    conn.execute(text("INSERT INTO talent_networks (slug, status) VALUES ('turing', 'vetted')"))
    conn.execute(
        sa.table("app_settings", sa.column("key"), sa.column("value", sa.JSON)).insert(),
        {"key": "telegram", "value": {"paused": True}},
    )


def _check_upgraded(conn, *, profile_expected: bool):
    (owner,) = conn.execute(text("SELECT id, telegram_chat_id, role, profile, paused FROM users")).all()
    assert owner.telegram_chat_id == "424242" and owner.role == "owner" and bool(owner.paused)
    prof = json.loads(owner.profile) if isinstance(owner.profile, str) else owner.profile
    if profile_expected:
        assert prof["name"] == "From env" and prof["skills"] == {"python": 3}
    else:
        assert prof is None
    apps = conn.execute(text("SELECT user_id, job_id, status, notes FROM applications ORDER BY job_id")).all()
    assert [tuple(a) for a in apps] == [(owner.id, 1, "applied", "note"), (owner.id, 2, "not_interested", None)]
    talent = conn.execute(text("SELECT user_id, slug, status FROM talent_networks")).all()
    assert [tuple(t) for t in talent] == [(owner.id, "turing", "vetted")]
    assert conn.execute(text("SELECT count(*) FROM app_settings WHERE key = 'telegram'")).scalar() == 0
    return owner.id


def _add_member_rows(conn, owner_id):
    conn.execute(text("INSERT INTO users (telegram_chat_id, role, paused) VALUES ('555', 'member', false)"))
    member = conn.execute(text("SELECT id FROM users WHERE role = 'member'")).scalar()
    conn.execute(text("INSERT INTO applications (user_id, job_id, status) VALUES (:m, 1, 'saved')"), {"m": member})
    conn.execute(
        text("INSERT INTO talent_networks (user_id, slug, status) VALUES (:m, 'turing', 'applied')"), {"m": member}
    )
    return member


def _check_downgraded(conn):
    apps = conn.execute(text("SELECT job_id, status, notes FROM applications ORDER BY job_id")).all()
    assert [tuple(a) for a in apps] == [(1, "applied", "note"), (2, "not_interested", None)]  # the owner's only
    assert [tuple(t) for t in conn.execute(text("SELECT slug, status FROM talent_networks")).all()] == [
        ("turing", "vetted")
    ]
    value = conn.execute(text("SELECT value FROM app_settings WHERE key = 'telegram'")).scalar()
    assert (json.loads(value) if isinstance(value, str) else value) == {"paused": True}
    assert not OWN_TABLES - {"applications", "talent_networks", "app_settings"} & set(inspect(conn).get_table_names())


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 424242 ")
    monkeypatch.setenv("PROFILE_YAML", PROFILE)


def test_sqlite_data_migration_round_trip(tmp_path, env):
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "003_tracker")
        _seed_003(conn)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "004_multi_user")
        owner_id = _check_upgraded(conn, profile_expected=True)
        _add_member_rows(conn, owner_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO applications (user_id, job_id) VALUES (:o, 1)"), {"o": owner_id})
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "003_tracker")
        _check_downgraded(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:  # job_id is unique again
        conn.execute(text("INSERT INTO applications (job_id) VALUES (1)"))
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
        _check_upgraded(conn, profile_expected=True)


def test_without_profile_env_the_owner_uses_the_deployment_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "424242")
    monkeypatch.delenv("PROFILE_YAML", raising=False)
    monkeypatch.setenv("PROFILE_PATH", str(tmp_path / "missing.yaml"))
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "003_tracker")
        _seed_003(conn)
        command.upgrade(alembic_config(conn), "head")
        _check_upgraded(conn, profile_expected=False)


def test_profile_file_is_read_when_there_is_no_profile_yaml(tmp_path, monkeypatch):
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "424242")
    monkeypatch.delenv("PROFILE_YAML", raising=False)
    monkeypatch.setenv("PROFILE_PATH", str(path))
    engine = sqlite_engine(tmp_path / "m.db")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "003_tracker")
        _seed_003(conn)
        command.upgrade(alembic_config(conn), "head")
        _check_upgraded(conn, profile_expected=True)


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_data_migration_round_trip(env):
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
                command.upgrade(alembic_config(conn), "003_tracker")
            else:
                older = [t for t in Base.metadata.sorted_tables if t.name not in OWN_TABLES | {"embeddings"}]
                Base.metadata.create_all(conn, tables=older)
                command.stamp(alembic_config(conn), "002")
                command.upgrade(alembic_config(conn), "003_tracker")
            _seed_003(conn)
            command.upgrade(alembic_config(conn), "head")
            owner_id = _check_upgraded(conn, profile_expected=True)
            _add_member_rows(conn, owner_id)
            uniques = {tuple(u["column_names"]) for u in inspect(conn).get_unique_constraints("applications")}
            assert ("user_id", "job_id") in uniques
            pk = inspect(conn).get_pk_constraint("talent_networks")["constrained_columns"]
            assert pk == ["user_id", "slug"]
            command.downgrade(alembic_config(conn), "003_tracker")
            _check_downgraded(conn)
            command.upgrade(alembic_config(conn), "head")
            _check_upgraded(conn, profile_expected=True)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()
