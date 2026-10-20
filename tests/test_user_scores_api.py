"""GET /api/jobs ranks, filters and scores by the viewer's own user_job_scores; without a login it answers
401. One user's token never yields another user's scores or statuses."""

import json
import os
import time

import main
import pytest
from app import tracker, users
from app.auth import Viewer, require_viewer
from app.config import settings as app_settings
from app.database import get_db
from app.db_sync import get_sync_db
from app.models import UserJobScore
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.tracker_fixtures import NOW, AsyncSessionAdapter, add_job, add_member, owner

ADMIN = {"X-Admin-Token": "adm"}
JOIN = "LEFT OUTER JOIN user_job_scores ON user_job_scores.job_id = jobs_raw.id AND user_job_scores.user_id"


def _set_score(session, user_id, job_id, *, fit, rank, hire, eligible=True, marker=""):
    row = session.get(UserJobScore, (user_id, job_id))
    if row is None:
        row = UserJobScore(user_id=user_id, job_id=job_id, scored_at=NOW)
        session.add(row)
    row.score, row.rank, row.hire_probability, row.eligible = fit, rank, hire, eligible
    row.match = {"score": fit, "eligible": eligible, "reasons": [f"Reason {marker}"],
                 "matched_skills": [marker.lower()], "breakdown": {"role": 20}}  # fmt: skip
    row.hire = {"score": hire, "factors": {"freshness": 1.0}, "reasons": [f"Hire {marker}"]}


@pytest.fixture
def world(session):
    """Owner, member A and member B; four jobs whose order differs for each of them."""
    me = owner(session)
    a = add_member(session, "555001", name="Alem")
    b = add_member(session, "555002", name="Bethel")
    jobs = [
        add_job(session, f"Job {i}", score=50 + 10 * i, extra={"rank": 40 + 10 * i, "hire_probability": 50},
                eligible=None if i == 0 else True)
        for i in range(4)
    ]  # fmt: skip
    # Public (and owner): Job 3 > Job 2 > Job 1 > Job 0. A: Job 0 first, Job 3 not open to A. B: Job 1 first.
    for i, job in enumerate(jobs):
        _set_score(session, a.id, job.id, fit=90 - 10 * i, rank=95 - 10 * i, hire=80, eligible=i != 3, marker=f"A{i}")
        _set_score(session, b.id, job.id, fit=30 + i, rank=99 if i == 1 else 10 + i, hire=20, marker=f"B{i}")
    tracker.set_status(session, b.id, jobs[2].id, "applied", notes="B-SECRET-NOTE", now=NOW)
    tracker.set_status(session, a.id, jobs[1].id, "saved", now=NOW)
    tokens = {"a": users.issue_dashboard_token(session, a), "b": users.issue_dashboard_token(session, b)}
    session.commit()
    return {"owner": me, "a": a, "b": b, "jobs": jobs, "tokens": tokens}


@pytest.fixture
def client(engine, monkeypatch):
    def sync_db():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    async def async_db():
        with Session(engine, expire_on_commit=False) as s:
            yield AsyncSessionAdapter(s)

    monkeypatch.setattr(app_settings, "admin_token", "adm")
    main.app.dependency_overrides[get_sync_db] = sync_db
    main.app.dependency_overrides[get_db] = async_db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _get(client, token=None, **params):
    headers = {"X-User-Token": token} if token else {}
    resp = client.get("/api/jobs", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _titles(body):
    return [j["title"] for j in body["items"]]


def test_the_public_gets_401_and_no_scores(client, world):
    for params in ({}, {"sort": "rank"}, {"sort": "score", "min_score": 0.65}, {"page_size": 1}):
        resp = client.get("/api/jobs", params=params)
        assert resp.status_code == 401 and "Job " not in resp.text and "total" not in resp.json()


def test_a_viewer_without_rows_gets_the_public_score_marked_public(client, world, session):
    session.query(UserJobScore).filter(UserJobScore.user_id == world["owner"].id).delete()
    session.commit()
    body = client.get("/api/jobs", params={"sort": "rank"}, headers=ADMIN).json()
    assert _titles(body) == ["Job 3", "Job 2", "Job 1", "Job 0"]
    job3 = body["items"][0]["analysis"]
    assert job3["score"] == pytest.approx(0.8) and job3["extracted_json"]["rank"] == 70
    assert {j["analysis"]["extracted_json"]["score_scope"] for j in body["items"]} == {"public"}
    assert "Reason A" not in json.dumps(body) and "Reason B" not in json.dumps(body)


def test_a_token_ranks_and_scores_by_that_users_own_rows(client, world):
    body = _get(client, world["tokens"]["a"], sort="rank")
    assert _titles(body) == ["Job 0", "Job 1", "Job 2", "Job 3"]
    first = body["items"][0]["analysis"]
    assert first["score"] == pytest.approx(0.9) and first["skills"] == ["a0"]
    ej = first["extracted_json"]
    assert ej["score_scope"] == "user" and ej["rank"] == 95 and ej["hire_probability"] == 80
    assert ej["match"]["reasons"] == ["Reason A0"] and ej["match"]["score"] == 90
    assert ej["hire"]["reasons"] == ["Hire A0"]
    # B's ranking is different, from B's rows.
    assert _titles(_get(client, world["tokens"]["b"], sort="rank")) == ["Job 1", "Job 3", "Job 2", "Job 0"]
    assert _titles(_get(client, world["tokens"]["b"], sort="score")) == ["Job 3", "Job 2", "Job 1", "Job 0"]


def test_min_score_uses_the_viewers_fit(client, world):
    a = world["tokens"]["a"]
    assert _titles(_get(client, a, sort="score", min_score=0.75)) == ["Job 0", "Job 1"]
    assert _get(client, world["tokens"]["b"], min_score=0.5)["total"] == 0
    owner_view = client.get("/api/jobs", params={"sort": "score", "min_score": 0.75}, headers=ADMIN).json()
    assert _titles(owner_view) == ["Job 3"]  # no rows for the owner: the public fit


def test_private_eligibility_filter_reads_the_viewers_row_then_the_public_flag(monkeypatch):
    """The SQL (the JSONB path only runs on Postgres; behaviour is checked in the Postgres test below)."""
    from app.models import JobRaw
    from app.routes.jobs import _join_user_scores, user_eligibility_filter
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    stmt = _join_user_scores(select(JobRaw.id), 7).where(user_eligibility_filter({"true", "unknown"}))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert f"{JOIN} = 7" in sql
    assert "user_job_scores.eligible IS true" in sql
    assert "user_job_scores.job_id IS NOT NULL AND user_job_scores.eligible IS NULL" in sql
    assert "user_job_scores.job_id IS NULL AND (jobs_raw.id IN (SELECT jobs_analysis.job_id" in sql
    assert "#>> '{match, eligible}'" in sql


def test_no_cross_user_leakage(client, world):
    a, b = world["tokens"]["a"], world["tokens"]["b"]
    for params in ({}, {"sort": "rank"}, {"sort": "score"}, {"sort": "recent"}, {"status": "new,saved,applied"}):
        text_a = json.dumps(_get(client, a, **params))
        assert "Reason B" not in text_a and "Hire B" not in text_a and "B-SECRET-NOTE" not in text_a
        text_b = json.dumps(_get(client, b, **params))
        assert "Reason A" not in text_b and "Hire A" not in text_b
    # Status filters only see the viewer's own tracker.
    assert _get(client, a, status="applied")["total"] == 0
    assert _titles(_get(client, b, status="applied")) == ["Job 2"]
    assert _titles(_get(client, a, status="saved")) == ["Job 1"]
    for job in world["jobs"]:
        detail = client.get(f"/api/jobs/{job.id}", headers={"X-User-Token": a}).json()
        assert detail["analysis"]["extracted_json"]["match"]["reasons"][0].startswith("Reason A")
    # A wrong token is nobody: 401, not someone's view.
    resp = client.get("/api/jobs", headers={"X-User-Token": "not-a-real-token-123456"})
    assert resp.status_code == 401 and "Job " not in resp.text


def test_the_owner_sees_their_own_rows(client, world, session):
    me, job0 = world["owner"], world["jobs"][0]
    _set_score(session, me.id, job0.id, fit=99, rank=99, hire=99, marker="OWNER")
    session.commit()
    body = client.get("/api/jobs", params={"sort": "rank"}, headers=ADMIN).json()
    assert body["items"][0]["title"] == "Job 0"
    assert body["items"][0]["analysis"]["extracted_json"]["match"]["reasons"] == ["Reason OWNER"]
    assert _get(client, world["tokens"]["a"], sort="rank")["items"][0]["title"] == "Job 0"  # A's view is unaffected


def test_jobs_without_a_row_for_the_viewer_keep_the_public_score_and_sort_last(client, world, session):
    late = add_member(session, "555003", name="Late")
    token = users.issue_dashboard_token(session, late)
    old = add_job(session, "Old job", score=99, extra={"rank": 99})
    session.query(UserJobScore).filter(UserJobScore.user_id == late.id).delete()
    _set_score(session, late.id, world["jobs"][1].id, fit=10, rank=5, hire=5, marker="L")
    session.commit()
    body = _get(client, token, sort="rank")
    assert _titles(body)[0] == "Job 1"  # the only job scored for this user, despite rank 5
    assert set(_titles(body)[1:]) == {"Old job", "Job 0", "Job 2", "Job 3"} and _titles(body)[1] == "Old job"
    scopes = {j["title"]: j["analysis"]["extracted_json"]["score_scope"] for j in body["items"]}
    assert scopes["Job 1"] == "user" and scopes["Old job"] == "public"
    assert old.id in {j["id"] for j in body["items"]}


# ---------------------------------------------------------------- Postgres: the private query stays cheap

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = "ff_user_scores_api_test"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_private_jobs_query_on_postgres_uses_the_index_and_three_statements():
    """1300 jobs, two users: the token request is the same three statements as the public one (count, page,
    analyses) and reads user_job_scores through its (user_id, job_id) primary key."""
    import asyncio

    from app.database import Base, async_database_url, sync_database_url
    from app.models import JobAnalysis, JobRaw, Source, User
    from sqlalchemy import create_engine, event, insert, text
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    admin = create_engine(sync_database_url(TEST_DATABASE_URL))
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    scoped = create_engine(
        sync_database_url(TEST_DATABASE_URL), connect_args={"options": f"-csearch_path={SCHEMA},public"}
    )
    url, connect_args = async_database_url(TEST_DATABASE_URL)
    aengine = create_async_engine(
        url, poolclass=NullPool, connect_args={**connect_args, "server_settings": {"search_path": SCHEMA}}
    )
    try:
        tables = [t for t in Base.metadata.sorted_tables if t.name != "embeddings"]
        Base.metadata.create_all(scoped, tables=tables)
        n = 1300
        with Session(scoped) as s:
            src = Source(name="t", base_url="https://example.com")
            u1, u2 = User(role="owner", display_name="Owner"), User(role="member", display_name="Friend")
            s.add_all([src, u1, u2])
            s.flush()
            s.execute(insert(JobRaw), [dict(id=i, source_id=src.id, url=f"https://x/{i}", title=f"Job {i}",
                                            content_hash=str(i), description="d" * 500)
                                       for i in range(1, n + 1)])  # fmt: skip
            s.execute(insert(JobAnalysis), [dict(job_id=i, score=(i % 100) / 100, extracted_json={
                "rank": i % 100, "match": {"eligible": i % 3 != 0}}) for i in range(1, n + 1)])  # fmt: skip
            s.execute(insert(UserJobScore), [dict(user_id=u.id, job_id=i, score=(i * k) % 100, rank=(i * k) % 97,
                                                  eligible=i % 4 != 0, match={}, hire={})
                                             for u, k in ((u1, 7), (u2, 13)) for i in range(1, n + 1)])  # fmt: skip
            s.commit()
            friend_id = u2.id
        with scoped.begin() as conn:
            conn.execute(text("ANALYZE"))

        statements: list[str] = []
        event.listen(aengine.sync_engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
        factory = async_sessionmaker(aengine, class_=AsyncSession)

        async def real_db():
            async with factory() as session:
                yield session

        captured = []

        class Capture:
            def __init__(self, inner):
                self.inner = inner

            async def execute(self, stmt, *a, **kw):
                captured.append(stmt)
                return await self.inner.execute(stmt, *a, **kw)

        async def capturing_db():
            async for session in real_db():
                yield Capture(session)

        main.app.dependency_overrides[get_db] = capturing_db
        main.app.dependency_overrides[require_viewer] = lambda: Viewer(role="member", user_id=friend_id)
        client = TestClient(main.app, headers={"X-User-Token": "friend"})
        client.get("/api/jobs", params={"sort": "rank", "eligible": "true,unknown"})  # warm up
        statements.clear()
        captured.clear()
        started = time.perf_counter()
        body = client.get("/api/jobs", params={"sort": "rank", "eligible": "true,unknown", "min_score": 0.1}).json()
        elapsed = time.perf_counter() - started
        assert len(body["items"]) == 20 and body["total"] > 0
        assert all(j["analysis"]["extracted_json"]["score_scope"] == "user" for j in body["items"])
        ranks = [j["analysis"]["extracted_json"]["rank"] for j in body["items"]]
        assert ranks == sorted(ranks, reverse=True) and ranks[0] == 96
        assert len(statements) == 3, statements  # count, page, selectinload of the analyses
        assert elapsed < 1.0

        def total(**params):
            return client.get("/api/jobs", params={"page_size": 1, **params}).json()["total"]

        # Eligibility is the viewer's (i % 4 == 0 not open), not the public flag (i % 3 == 0).
        assert total(eligible="false") == n // 4 and total(eligible="true,unknown") == n - n // 4
        assert total(eligible="unknown") == 0
        # Without a row for the viewer the public flag decides: jobs 3 and 6 are open to the viewer but not
        # to the public profile.
        with Session(scoped) as s:
            s.query(UserJobScore).filter(UserJobScore.user_id == friend_id, UserJobScore.job_id.in_([3, 6])).delete()
            s.commit()
        assert total(eligible="false") == n // 4 + 2

        page_sql = str(captured[1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert f"{JOIN} = {friend_id}" in page_sql

        async def explain(sql):
            async with factory() as session:
                await session.execute(text("SET enable_seqscan = off"))  # what the planner does at scale
                return "\n".join(r[0] for r in (await session.execute(text("EXPLAIN " + sql))).all())

        plan = asyncio.run(explain(page_sql))
        assert "user_job_scores_pkey" in plan or "ix_user_job_scores_user_rank" in plan, plan
        assert "Seq Scan on user_job_scores" not in plan, plan
    finally:
        main.app.dependency_overrides.clear()
        scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        admin.dispose()


def test_per_user_enrichment_counts_are_for_the_owner_only(client, world, session):
    from app.models import Run

    stats = {"selected": 4, "enriched": 4, "selected_by_user": {"1": 2, "2": 2}, "enriched_by_user": {"1": 2, "2": 2}}
    session.add(Run(type="llm_enrich", status="completed", started_at=NOW, stats=stats))
    session.commit()

    def enrich_stats(headers):
        (run,) = [r for r in client.get("/api/runs", headers=headers).json() if r["type"] == "llm_enrich"]
        return run["stats"]

    assert client.get("/api/runs").status_code == 401
    assert enrich_stats({"X-User-Token": world["tokens"]["a"]}) == {"selected": 4, "enriched": 4}
    assert enrich_stats(ADMIN) == stats
