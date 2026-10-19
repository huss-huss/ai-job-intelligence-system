"""CLI: `llm-eval --dry-run` estimates without calling, `--run` prints disagreements and cost, `set-resume`."""

import json

import pytest
from app.models import LlmUsage, User
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.llm_fakes import EXTRACTION, FakeOpenAI
from tests.tracker_fixtures import add_job, owner
from worker import cli, llm, llm_eval
from worker.config import settings as worker_settings
from worker.llm import Price

DESC = "We build FastAPI services on PostgreSQL. You will design APIs and own deployments. " * 30  # ~2.5k chars


@pytest.fixture
def db_url(engine, session, monkeypatch, tmp_path):
    llm.forget_learned()
    for i in range(24):
        eligible = (True, False, None)[i % 3]
        location = {True: "Remote (Worldwide)", False: "USA only", None: "Remote"}[eligible]
        add_job(session, f"Backend Engineer {i}", eligible=eligible, location=location, description=DESC)
    for key, value in (("openai_api_key", "sk-test"), ("llm_base_url", ""), ("openai_model", "gpt-6-luna"),
                       ("llm_prices_json", ""), ("profile_yaml", ""), ("llm_monthly_budget_usd", 0.80)):
        monkeypatch.setattr(worker_settings, key, value)  # fmt: skip
    monkeypatch.setattr(worker_settings, "profile_path", "/nonexistent/profile.yaml")
    return str(engine.url)


def test_pick_jobs_mixes_eligible_restricted_and_unknown(session, db_url):
    jobs = llm_eval.pick_jobs(session, 20)
    assert len(jobs) == 20
    locations = [j.location for j in jobs]
    assert {locations.count(x) for x in ("Remote (Worldwide)", "USA only", "Remote")} == {7, 6}


def test_eval_dry_run_prints_the_estimate_and_sends_nothing(db_url, capsys, monkeypatch, session):
    def no_client(*a, **k):
        raise AssertionError("--dry-run must not build an API client")

    monkeypatch.setattr(llm, "make_client", no_client)
    assert cli.main(["llm-eval", "--jobs", "20", "--dry-run", "--database-url", db_url]) == 0
    out = capsys.readouterr().out
    from worker.tasks import _to_posting

    est = llm_eval.estimate([_to_posting(j) for j in llm_eval.pick_jobs(session, 20)], Price(0.10, 0.50))
    assert f"estimated cost: typical ${est.typical_usd:.4f}, worst case ${est.worst_usd:.4f}" in out
    assert "20 jobs · model gpt-6-luna" in out and "dry run: nothing was sent" in out
    assert est.worst_usd < 0.02 and est.typical_usd < 0.01  # the owner's one-time run is well under a cent
    assert session.scalars(select(LlmUsage)).all() == []


def test_eval_run_prints_disagreements_tokens_and_cost(db_url, capsys, monkeypatch, session):
    def respond(body):
        user = body["messages"][1]["content"]
        # The LLM reads "Remote" postings as US-only and calls everything a contract.
        restriction = "United States" if "Location: Remote\n" in user else None
        return json.dumps({**EXTRACTION, "location_restriction": restriction, "employment_type": "contract"})

    fake = FakeOpenAI(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda settings, **kw: fake.client())
    assert cli.main(["llm-eval", "--jobs", "6", "--run", "--database-url", db_url]) == 0
    out = capsys.readouterr().out
    assert len(fake.requests) == 6
    assert "6 of 6 jobs differ" in out and "eligibility 2" in out and "employment 6" in out
    assert "tokens: 3000 in · 1800 out (720 reasoning) · cost $0.00120" in out
    usage = session.scalars(select(LlmUsage)).all()
    assert len(usage) == 6 and {u.feature for u in usage} == {"eval"}  # not counted against the daily cap


def test_eval_refuses_when_the_worst_case_exceeds_the_budget_left(db_url, capsys, monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(llm, "make_client", lambda settings, **kw: fake.client())
    monkeypatch.setattr(worker_settings, "llm_monthly_budget_usd", 0.001)
    assert cli.main(["llm-eval", "--jobs", "20", "--run", "--database-url", db_url]) == 1
    assert fake.requests == [] and "Refusing" in capsys.readouterr().err


def test_set_resume_stores_the_owners_resume(engine, session, tmp_path, monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "424242")
    owner(session)
    pdf = Path(__file__).parent / "fixtures" / "resume.pdf"
    assert cli.main(["set-resume", str(pdf), "--database-url", str(engine.url)]) == 0
    assert "Stored the owner's resume" in capsys.readouterr().out
    with Session(engine) as s:
        me = s.scalars(select(User).where(User.role == "owner")).one()
        assert "Example Logistics" in me.resume_text and me.resume_updated_at is not None
    bad = tmp_path / "notes.docx"
    bad.write_bytes(b"x" * 300)
    assert cli.main(["set-resume", str(bad), "--database-url", str(engine.url)]) == 1
