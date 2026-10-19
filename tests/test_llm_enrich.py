"""LLM extraction runs only on the day's top-N jobs by rules rank, within the daily cap and the budget."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.models import JobAnalysis, JobRaw, LlmUsage, Run, Source, UserJobScore
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.llm_fakes import EXTRACTION, FakeOpenAI
from tests.tracker_fixtures import add_member, owner
from worker import llm, tasks
from worker.ai import HeuristicExtractor, LLMExtractor
from worker.budget import BudgetGuard, MemoryUsageStore
from worker.config import settings as worker_settings
from worker.llm import LLMCaller, LLMConfig
from worker.pipeline import analyze, discover, select_for_llm
from worker.profile import load_profile

ROOT = Path(__file__).resolve().parent.parent
PROFILE = load_profile(ROOT / "profile.example.yaml")
NOW = datetime(2026, 10, 19, tzinfo=UTC)
GOOD_DESC = "Python, FastAPI, PostgreSQL, Redis, Docker and LLM agents for our platform. " * 8
TITLES = [
    ("Senior Backend Engineer", "Remote (Worldwide)", GOOD_DESC),  # best by rules
    ("Senior Python Engineer", "Remote", GOOD_DESC),  # unknown eligibility; the LLM will read "US only"
    ("Backend Developer", "Remote", "Python services. " * 30),
    ("Sales Representative", "Remote", "Sell our product. " * 30),  # excluded title, fit 0: never sent to the LLM
]


@pytest.fixture(autouse=True)
def _fresh_learning():
    llm.forget_learned()


def test_select_for_llm_takes_the_best_by_rank_and_skips_zero_fit():
    from worker.sources.base import JobPosting

    jobs = [
        analyze(JobPosting(source="t", external_id=str(i), url=f"https://x/{i}", title=t, location=loc,
                           remote=True, description=d, posted_at=NOW), PROFILE, HeuristicExtractor(), now=NOW)
        for i, (t, loc, d) in enumerate(TITLES)
    ]  # fmt: skip
    assert jobs[3].score == 0
    picked = select_for_llm(jobs, 2)
    assert picked == sorted(range(3), key=lambda i: (jobs[i].rank, jobs[i].score), reverse=True)[:2]
    assert select_for_llm(jobs, 10) == sorted(range(3), key=lambda i: (jobs[i].rank, jobs[i].score), reverse=True)
    assert select_for_llm(jobs, 0) == []


def test_discover_extracts_only_the_top_n_with_the_llm(monkeypatch):
    from worker import pipeline
    from worker.sources.base import JobPosting

    postings = [
        JobPosting(source="t", external_id=str(i), url=f"https://x/{i}", title=t, location=loc, remote=True,
                   description=d, posted_at=NOW)
        for i, (t, loc, d) in enumerate(TITLES)
    ]  # fmt: skip
    monkeypatch.setattr(pipeline, "fetch_postings", lambda config, http: postings)
    fake = FakeOpenAI()
    guard = BudgetGuard(MemoryUsageStore(), max_extractions_per_day=2)
    extractor = LLMExtractor(LLMCaller(fake.client(), LLMConfig(model="gpt-6-luna"), guard))
    report = discover([{"type": "fixture"}], PROFILE, extractor, now=NOW)
    assert len(fake.requests) == 2 and report.llm_calls == 2
    methods = {j.posting.title: j.extraction.method for j in report.jobs}
    assert sorted(t for t, m in methods.items() if m == "llm") == ["Senior Backend Engineer", "Senior Python Engineer"]
    assert report.llm_cost_usd > 0 and report.reasoning_tokens == 240


@pytest.fixture
def db(engine, monkeypatch):
    @contextmanager
    def factory():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    monkeypatch.setattr(tasks, "get_sync_session", factory)
    for key in ("notion_token", "alert_webhook_url", "profile_yaml", "llm_base_url", "llm_prices_json"):
        monkeypatch.setattr(worker_settings, key, "")
    monkeypatch.setattr(worker_settings, "profile_path", "/nonexistent/profile.yaml")  # bundled example
    monkeypatch.setattr(worker_settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(worker_settings, "openai_model", "gpt-6-luna")
    monkeypatch.setattr(worker_settings, "llm_max_extractions_per_day", 2)
    monkeypatch.setattr(worker_settings, "llm_monthly_budget_usd", 0.80)
    return lambda: Session(engine, expire_on_commit=False)


def _store_jobs(session) -> list[int]:
    source = Source(name="Fixture", base_url="https://example.com")
    session.add(source)
    session.flush()
    ids = []
    for i, (title, location, desc) in enumerate(TITLES):
        job = JobRaw(source_id=source.id, url=f"https://jobs.example.com/{i}", title=title, company="Acme",
                     location=location, description=desc, content_hash=f"h{i}", meta={"source": "fixture",
                     "remote": True})  # fmt: skip
        session.add(job)
        session.flush()
        ids.append(job.id)
    session.commit()
    return ids


def _respond(body: dict) -> str:
    """US-only for the "Senior Python Engineer" posting, worldwide otherwise."""
    user = body["messages"][1]["content"]
    us_only = "Senior Python Engineer" in user
    return json.dumps({**EXTRACTION, "location_restriction": "United States" if us_only else None,
                       "worldwide": None if us_only else True})  # fmt: skip


def test_daily_run_extracts_the_top_n_and_rescores_every_user(session, db):
    me = owner(session)
    friend = add_member(session, "555001")
    ids = _store_jobs(session)
    tasks.run_analyze_task(ids)  # rules only
    session.expire_all()
    assert {a.extracted_json["method"] for a in session.scalars(select(JobAnalysis))} == {"heuristic"}

    fake = FakeOpenAI(respond=_respond)
    result = tasks.run_llm_enrich_task(client=fake.client(), session_factory=db)
    assert result["candidates"] == 3 and result["selected"] == 2 and result["enriched"] == 2
    assert result["llm_calls"] == 2 and result["llm_fallbacks"] == {} and len(fake.requests) == 2
    assert 0 < result["month_to_date_usd"] < 0.01 and result["budget_usd"] == 0.80

    session.expire_all()
    joined = session.execute(select(JobRaw, JobAnalysis).join(JobAnalysis, JobAnalysis.job_id == JobRaw.id))
    by_title = {job.title: analysis.extracted_json for job, analysis in joined}
    assert by_title["Senior Backend Engineer"]["method"] == "llm"
    assert by_title["Senior Backend Engineer"]["llm_model"] == "gpt-6-luna"
    python = by_title["Senior Python Engineer"]
    assert python["method"] == "llm" and python["match"]["eligible"] is False
    assert "AI read: applicants must be in the United States" in python["match"]["reasons"]
    assert by_title["Backend Developer"]["method"] == "heuristic"  # outside the top 2
    python_id = ids[1]
    for user in (me, friend):
        row = session.get(UserJobScore, (user.id, python_id))
        assert row.eligible is False and "AI read: applicants must be in the United States" in row.match["reasons"]

    usage = session.scalars(select(LlmUsage)).all()
    assert len(usage) == 2 and {u.feature for u in usage} == {"extract"} and all(u.status == "recorded" for u in usage)
    assert all(u.reasoning_tokens == 120 for u in usage)
    run = session.scalars(select(Run).where(Run.type == "llm_enrich")).one()
    assert run.status == "completed" and run.stats["enriched"] == 2

    # Same day again: the cap is used up, so nothing is sent.
    again = tasks.run_llm_enrich_task(client=fake.client(), session_factory=db)
    assert again["selected"] == 0 and len(fake.requests) == 2

    # Rescoring (POST /api/analyze) keeps the LLM's facts instead of falling back to rules.
    tasks.run_analyze_task(ids)
    session.expire_all()
    kept = session.scalars(select(JobAnalysis).where(JobAnalysis.job_id == python_id)).one().extracted_json
    assert kept["method"] == "llm" and kept["llm_model"] == "gpt-6-luna" and kept["match"]["eligible"] is False
    assert len(fake.requests) == 2


def test_failed_calls_are_logged_counted_and_not_retried(session, db, caplog):
    owner(session)
    ids = _store_jobs(session)
    tasks.run_analyze_task(ids)
    fake = FakeOpenAI(content="not json")
    result = tasks.run_llm_enrich_task(client=fake.client(), session_factory=db)
    assert result["enriched"] == 0 and result["llm_fallbacks"] == {"invalid_output": 2}
    assert "kept the rules extraction" in caplog.text and "invalid output" in caplog.text
    session.expire_all()
    marked = [a.extracted_json.get("llm_fallback") for a in session.scalars(select(JobAnalysis))]
    assert marked.count("invalid_output") == 2
    # Billed, so recorded; and the two failed jobs aren't picked again.
    assert len(session.scalars(select(LlmUsage)).all()) == 2
    assert [jid for _, _, jid in tasks.llm_candidates(session, datetime.now(UTC))] == [ids[2]]


def test_budget_refusal_sends_nothing(session, db, monkeypatch):
    owner(session)
    ids = _store_jobs(session)
    tasks.run_analyze_task(ids)
    monkeypatch.setattr(worker_settings, "llm_monthly_budget_usd", 0.0)
    fake = FakeOpenAI()
    result = tasks.run_llm_enrich_task(client=fake.client(), session_factory=db)
    assert fake.requests == [] and result["llm_fallbacks"] == {"budget": 2} and result["llm_calls"] == 0


def test_enrich_is_skipped_without_a_key(db, monkeypatch):
    monkeypatch.setattr(worker_settings, "openai_api_key", "")
    assert "skipped" in tasks.run_llm_enrich_task(session_factory=db)
