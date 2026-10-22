"""A disabled source's jobs are hidden everywhere a user sees jobs: the dashboard list (and its count),
/top, /today and the daily digest. Migration 008 disables We Work Remotely (pay-to-apply listings)."""

from alembic import command
from app import tracker
from app.models import Source
from app.telegram import bot
from sqlalchemy import text

from tests.sqlite_support import alembic_config, sqlite_engine
from tests.test_web_login import api, two  # noqa: F401 - fixtures
from tests.tracker_fixtures import NOW, message_update, settings

ADMIN = {"X-Admin-Token": "adm"}


def _wwr(session, jobs):
    """Move the first job to a disabled We Work Remotely source."""
    source = Source(name="weworkremotely", base_url="https://weworkremotely.com", config={"type": "weworkremotely"})
    source.enabled = False
    session.add(source)
    session.flush()
    jobs[0].source_id = source.id
    session.commit()
    return source, jobs[0]


def test_candidates_skip_disabled_sources(session, two):  # noqa: F811
    owner, _, _, jobs = two
    before = {c.job.id for c in tracker.candidates(session, owner.id)}
    source, hidden = _wwr(session, jobs)
    after = {c.job.id for c in tracker.candidates(session, owner.id)}
    assert hidden.id in before and after == before - {hidden.id}
    source.enabled = True
    session.commit()
    assert {c.job.id for c in tracker.candidates(session, owner.id)} == before  # re-enabling brings them back


def test_top_and_today_skip_disabled_sources(session, two, fake_tg):  # noqa: F811
    _, _, _, jobs = two
    _, hidden = _wwr(session, jobs)
    for command_text in ("/top", "/today"):
        fake_tg.requests.clear()
        bot.handle_update(session, fake_tg.client(), settings(), message_update(command_text), now=NOW)
        sent = " ".join(body.get("text", "") for _, body in fake_tg.requests)
        assert hidden.title not in sent and f"#{hidden.id}" not in sent, command_text
        assert any(j.title in sent for j in jobs[1:]), command_text


def test_dashboard_list_and_count_skip_disabled_sources(api, session, two):  # noqa: F811
    _, _, _, jobs = two
    before = api.get("/api/jobs", headers=ADMIN).json()
    _, hidden = _wwr(session, jobs)
    after = api.get("/api/jobs", headers=ADMIN).json()
    assert after["total"] == before["total"] - 1
    assert hidden.id not in {j["id"] for j in after["items"]}


def test_008_disables_weworkremotely_and_downgrade_restores_it(tmp_path):
    engine = sqlite_engine(tmp_path / "m.db")
    insert = text("INSERT INTO sources (name, base_url, config, enabled) VALUES (:n, :u, :c, true)")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "007_login_links")
        wwr = '{"type": "weworkremotely"}'
        conn.execute(insert, {"n": "We Work Remotely", "u": "https://weworkremotely.com", "c": wwr})
        conn.execute(insert, {"n": "remotive", "u": "https://remotive.com", "c": '{"type": "remotive"}'})
        conn.execute(insert, {"n": "weworkremotely", "u": "https://weworkremotely.com", "c": None})
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "008_disable_weworkremotely")
        enabled = dict(conn.execute(text("SELECT base_url || '|' || name, enabled FROM sources")).all())
    assert {k: bool(v) for k, v in enabled.items()} == {
        "https://weworkremotely.com|We Work Remotely": False,
        "https://remotive.com|remotive": True,
        "https://weworkremotely.com|weworkremotely": False,
    }
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "007_login_links")
        assert all(v for (v,) in conn.execute(text("SELECT enabled FROM sources")).all())
    engine.dispose()
