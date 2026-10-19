"""Per-user scores: the analysis run scores every job for every user's profile, from facts extracted once."""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from app import users
from app.models import JobAnalysis, JobRaw, Source, UserJobScore
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.tracker_fixtures import NOW, add_job, add_member, owner
from worker import tasks, user_scoring
from worker.config import settings as worker_settings

DATA_PROFILE = {
    "target_titles": ["data engineer"],
    "skills": {"spark": 3, "airflow": 3, "sql": 2},
    "seniority": ["senior"],
}


@pytest.fixture
def analyze_env(engine, monkeypatch):
    @contextmanager
    def factory():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    monkeypatch.setattr(tasks, "get_sync_session", factory)
    for key in ("openai_api_key", "llm_base_url", "notion_token", "alert_webhook_url", "profile_yaml"):
        monkeypatch.setattr(worker_settings, key, "")
    monkeypatch.setattr(worker_settings, "profile_path", "/nonexistent/profile.yaml")  # bundled example


def _raw(session, title, description):
    source = session.query(Source).first() or Source(name="Fixture", base_url="https://example.com")
    session.add(source)
    session.flush()
    job = JobRaw(
        source_id=source.id,
        url=f"https://jobs.example.com/{title.replace(' ', '-')}",
        title=title,
        company="Acme",
        location="Remote (Worldwide)",
        description=description,
        content_hash=f"h-{title}",
        meta={"source": "fixture", "remote": True},
    )
    session.add(job)
    session.commit()
    return job


def test_analysis_scores_each_user_with_their_own_profile(session, analyze_env):
    me = owner(session)
    friend = add_member(session, "555001", profile=DATA_PROFILE)
    backend = _raw(session, "Senior Backend Engineer", "Python, FastAPI, Postgres and Redis. Remote worldwide.")
    data = _raw(session, "Senior Data Engineer", "Spark, Airflow and SQL pipelines. Remote worldwide.")
    result = tasks.run_analyze_task([backend.id, data.id])
    assert result["processed"] == 2 and result["users_scored"] == 2
    session.expire_all()
    rows = {(r.user_id, r.job_id): r for r in session.scalars(select(UserJobScore))}
    assert len(rows) == 4
    # The owner has no profile of their own: their row is exactly the public analysis.
    public = session.scalars(select(JobAnalysis).where(JobAnalysis.job_id == backend.id)).one()
    mine = rows[(me.id, backend.id)]
    assert mine.score == public.extracted_json["match"]["score"] and mine.rank == public.extracted_json["rank"]
    assert mine.hire_probability == public.extracted_json["hire_probability"]
    # The member's ranking follows their profile.
    assert rows[(friend.id, data.id)].score > rows[(friend.id, backend.id)].score
    assert rows[(me.id, backend.id)].score > rows[(me.id, data.id)].score
    assert set(rows[(friend.id, data.id)].hire) == {"score", "factors", "reasons"}


def test_score_missing_fills_gaps_once(session, analyze_env):
    me = owner(session)
    job = add_job(session, "Backend Engineer", description="Python")
    session.query(UserJobScore).delete()
    session.commit()
    assert user_scoring.score_missing(session, me, since=NOW - timedelta(days=30), now=NOW) == 1
    assert user_scoring.score_missing(session, me, since=NOW - timedelta(days=30), now=NOW) == 0
    row = session.get(UserJobScore, (me.id, job.id))
    assert row.score >= 0 and isinstance(row.match["reasons"], list)


def test_ensure_scores_covers_a_new_member(session, analyze_env):
    add_job(session, "Backend Engineer")
    friend = add_member(session, "555001")
    session.query(UserJobScore).filter(UserJobScore.user_id == friend.id).delete()
    session.commit()
    assert users.ensure_scores(session, friend, NOW) == 1


def test_stored_extraction_reuses_the_analysis_facts():
    analysis = JobAnalysis(extracted_json={"seniority": "senior", "risk_flags": ["unpaid"], "method": "llm"}, score=0)
    extraction = user_scoring.stored_extraction(analysis, None)
    assert extraction.method == "llm" and extraction.fields.risk_flags == ["unpaid"]


def test_an_invalid_member_profile_is_skipped_not_fatal(session):
    owner(session)
    bad = add_member(session, "555001", profile={"skills": {"python": 9}})
    assert [u.id for u, _ in user_scoring.user_profiles(session)] == [users.get_owner(session).id]
    assert bad.id not in [u.id for u, _ in user_scoring.user_profiles(session)]
