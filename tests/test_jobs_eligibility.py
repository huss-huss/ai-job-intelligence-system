"""GET /api/jobs?eligible=... filters on the stored match.eligible flag."""

import os

import main
import pytest
from app.auth import Viewer, require_viewer
from app.database import get_db
from app.models import JobAnalysis, JobRaw, User, UserJobScore
from app.routes.jobs import eligibility_filter, parse_eligible
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("value", ["any", "", "true,false,unknown", "unknown, any"])
def test_any_or_every_value_means_no_filter(value):
    assert parse_eligible(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", {"true"}), ("FALSE", {"false"}), ("true,unknown", {"true", "unknown"}), (" unknown ", {"unknown"})],
)
def test_parse_eligible_subsets(value, expected):
    assert parse_eligible(value) == expected


@pytest.mark.parametrize("value", ["yes", "true,maybe", "1"])
def test_parse_eligible_rejects_unknown_values(value):
    with pytest.raises(HTTPException) as exc:
        parse_eligible(value)
    assert exc.value.status_code == 422


def test_filter_reads_match_eligible_from_extracted_json():
    sql = _sql(select(JobRaw.id).where(eligibility_filter({"false"})))
    assert "jobs_analysis.extracted_json #>> '{match, eligible}'" in sql
    assert "= 'false'" in sql and "NOT IN" not in sql


def test_unknown_includes_null_and_unanalyzed_jobs():
    # "unknown" is "not known to be true or false", so jobs without an analysis row match too.
    sql = _sql(select(JobRaw.id).where(eligibility_filter({"true", "unknown"})))
    assert "jobs_raw.id IN (SELECT jobs_analysis.job_id" in sql
    assert "jobs_raw.id NOT IN (SELECT jobs_analysis.job_id" in sql
    assert "IN ('true', 'false')" in sql


class _Result:
    def __init__(self, scalar=0):
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingSession:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt, *_):
        self.statements.append(stmt)
        return _Result()


@pytest.fixture
def recording_client():
    session = _RecordingSession()

    async def fake_db():
        yield session

    # The list is private: a fixed viewer here (the token itself is tested in test_private_site.py).
    main.app.dependency_overrides[get_db] = fake_db
    main.app.dependency_overrides[require_viewer] = lambda: Viewer(role="owner", user_id=1)
    yield TestClient(main.app, headers={"X-User-Token": "viewer"}), session
    main.app.dependency_overrides.clear()


def test_default_request_is_unfiltered(recording_client):
    client, session = recording_client
    resp = client.get("/api/jobs")
    assert resp.status_code == 200 and resp.json()["total"] == 0
    for sql in map(_sql, session.statements):  # the viewer's score row is selected, but nothing filters on it
        assert "{match, eligible}" not in sql and "user_job_scores.eligible IS" not in sql


def test_eligible_param_filters_both_count_and_page(recording_client):
    client, session = recording_client
    assert client.get("/api/jobs", params={"eligible": "true,unknown"}).status_code == 200
    count_sql, page_sql = (_sql(s) for s in session.statements)
    for sql in (count_sql, page_sql):
        assert "#>> '{match, eligible}'" in sql and "NOT IN" in sql


def test_invalid_eligible_value_is_422(recording_client):
    client, _ = recording_client
    resp = client.get("/api/jobs", params={"eligible": "maybe"})
    assert resp.status_code == 422 and "eligible" in resp.json()["detail"]


# Optional end-to-end check against a real Postgres: TEST_DATABASE_URL=postgresql://... pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_eligible_filter_against_postgres():
    from app.database import Base, async_database_url, sync_database_url
    from app.models import Source
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    sync_engine = create_engine(sync_database_url(TEST_DATABASE_URL))
    tables = [Source.__table__, JobRaw.__table__, JobAnalysis.__table__, User.__table__, UserJobScore.__table__]
    Base.metadata.drop_all(sync_engine, tables=tables)
    Base.metadata.create_all(sync_engine, tables=tables)
    with Session(sync_engine) as s:
        src = Source(name="t", base_url="https://example.com")
        s.add(src)
        s.flush()
        for i, flag in enumerate([True, False, None, "missing", "unanalyzed"]):
            job = JobRaw(source_id=src.id, url=f"https://example.com/{i}", title=str(flag), content_hash=str(i))
            s.add(job)
            s.flush()
            if flag == "unanalyzed":
                continue
            match = {} if flag == "missing" else {"eligible": flag}
            extracted = {"match": match, **({"rank": [10, 90][i]} if i < 2 else {})}  # rank only on the first two
            s.add(JobAnalysis(job_id=job.id, extracted_json=extracted, score=0.5 - i / 10))
        s.commit()

    url, connect_args = async_database_url(TEST_DATABASE_URL)
    factory = async_sessionmaker(create_async_engine(url, poolclass=NullPool, connect_args=connect_args),
                                 class_=AsyncSession)

    async def real_db():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_db] = real_db
    # A viewer with no score rows: every job falls back to the public flag and score.
    main.app.dependency_overrides[require_viewer] = lambda: Viewer(role="owner", user_id=1)
    try:
        client = TestClient(main.app, headers={"X-User-Token": "viewer"})

        def titles(eligible=None):
            params = {"eligible": eligible} if eligible else {}
            body = client.get("/api/jobs", params=params).json()
            assert body["total"] == len(body["items"])
            return [j["title"] for j in body["items"]]

        assert titles() == ["True", "False", "None", "missing", "unanalyzed"]
        assert titles("true") == ["True"]
        assert titles("false") == ["False"]
        assert titles("unknown") == ["None", "missing", "unanalyzed"]
        assert titles("true,unknown") == ["True", "None", "missing", "unanalyzed"]
        ranked = client.get("/api/jobs", params={"sort": "rank"}).json()
        # stored rank first, then rows without one by fit score, unanalyzed last
        assert [j["title"] for j in ranked["items"]] == ["False", "True", "None", "missing", "unanalyzed"]
    finally:
        main.app.dependency_overrides.clear()
        Base.metadata.drop_all(sync_engine, tables=tables)


def test_sort_rank_orders_by_stored_rank_then_score(recording_client):
    client, session = recording_client
    assert client.get("/api/jobs", params={"sort": "rank"}).status_code == 200
    page_sql = _sql(session.statements[-1])
    order = page_sql.split("ORDER BY", 1)[1]
    assert "CAST(jobs_analysis.extracted_json ->> 'rank' AS FLOAT) DESC NULLS LAST" in order
    assert order.index("rank") < order.index("jobs_analysis.score DESC")


def test_unknown_sort_is_422(recording_client):
    client, _ = recording_client
    assert client.get("/api/jobs", params={"sort": "hire"}).status_code == 422
