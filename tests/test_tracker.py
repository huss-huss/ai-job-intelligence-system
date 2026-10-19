"""Tracker: status changes, candidate selection and the bounded, explainable feedback adjustment."""

from datetime import timedelta

import pytest
from app import tracker
from app.models import JobAnalysis
from app.tracker import (
    MAX_BOOST,
    MAX_PENALTY,
    JobFeatures,
    base_points,
    feedback_adjustment,
    hire_probability,
    similar,
    title_keywords,
)

from tests.tracker_fixtures import NOW, add_job, oid


def f(job_id, company, *keywords):
    return JobFeatures(job_id, "t", company, frozenset(keywords))


def test_title_keywords_ignore_seniority_and_filler():
    assert title_keywords("Senior Backend Engineer (Python) - Remote") == {"backend", "python"}
    assert title_keywords("Lead ML Platform Developer II") == {"ml", "platform"}


def test_similar_needs_two_shared_keywords_and_half_overlap():
    a = f(1, "a", "python", "fastapi", "backend")
    assert similar(a, f(2, "b", "python", "fastapi", "backend", "aws")) == {"python", "fastapi", "backend"}
    assert similar(a, f(3, "b", "python", "react", "frontend", "css")) is None  # one shared keyword
    assert similar(a, f(4, "b", "python", "fastapi", "x", "y", "z", "w")) is None  # 2 of 7: under 50%


def test_dislike_penalizes_same_company_and_similar_jobs_with_reasons():
    job = f(1, "acme", "python", "fastapi", "backend")
    adj = feedback_adjustment(job, liked=[], disliked=[f(9, "acme", "python", "fastapi", "backend")])
    assert adj.points == -20
    assert adj.reasons == [
        "Same company as a job you marked not for you",
        "Similar to jobs you marked not for you (backend, fastapi, python)",
    ]


def test_like_boosts_similar_jobs():
    job = f(1, "acme", "python", "fastapi", "backend")
    adj = feedback_adjustment(job, liked=[f(9, "other", "python", "fastapi", "backend")], disliked=[])
    assert adj.points == 5 and adj.reasons == ["Similar to jobs you saved or applied to (backend, fastapi, python)"]


def test_adjustments_are_bounded():
    job = f(1, "acme", "python", "fastapi", "backend")
    many = [f(i, "acme", "python", "fastapi", "backend") for i in range(10, 20)]
    assert feedback_adjustment(job, liked=[], disliked=many).points == -MAX_PENALTY
    assert feedback_adjustment(job, liked=many, disliked=[]).points == MAX_BOOST
    assert feedback_adjustment(job, liked=many, disliked=many).points == MAX_BOOST - MAX_PENALTY


def test_unrelated_history_changes_nothing_and_a_job_never_judges_itself():
    job = f(1, "acme", "python", "fastapi")
    assert feedback_adjustment(job, liked=[f(2, "x", "react", "css")], disliked=[f(3, "y", "go", "k8s")]).points == 0
    assert feedback_adjustment(job, liked=[], disliked=[f(1, "acme", "python", "fastapi")]).points == 0


def test_unknown_company_is_not_a_company_match():
    job = f(1, "", "python", "fastapi")
    assert feedback_adjustment(job, liked=[], disliked=[f(2, "", "go", "rust")]).points == 0


def test_base_points_prefer_rank_then_score():
    assert base_points(JobAnalysis(extracted_json={}, score=0.42)) == pytest.approx(42)
    assert base_points(JobAnalysis(extracted_json={"rank": 0.7}, score=0.42)) == pytest.approx(70)
    assert base_points(JobAnalysis(extracted_json={"rank": 88}, score=0.42)) == 88
    with_attr = JobAnalysis(extracted_json={"rank": 10}, score=0.42)
    with_attr.rank = 55  # a future column wins over the JSON copy
    assert base_points(with_attr) == 55
    assert hire_probability(JobAnalysis(extracted_json={"hire_probability": 34}, score=0)) == pytest.approx(0.34)
    assert hire_probability(JobAnalysis(extracted_json={}, score=0)) is None


def test_set_status_records_timestamps(session):
    job = add_job(session)
    app = tracker.set_status(session, oid(session), job.id, "saved", now=NOW)
    assert app.status == "saved" and app.status_changed_at == NOW and app.applied_at is None
    later = NOW + timedelta(hours=1)
    app = tracker.set_status(session, oid(session), job.id, "applied", notes="Sent CV", now=later)
    assert app.applied_at == later and app.notes == "Sent CV"
    app = tracker.set_status(session, oid(session), job.id, "interviewing", now=later + timedelta(days=2))
    assert app.applied_at == later  # first time only
    with pytest.raises(ValueError):
        tracker.set_status(session, oid(session), job.id, "ghosted")
    with pytest.raises(tracker.UnknownJob):
        tracker.set_status(session, oid(session), 99999, "saved")


def test_candidates_skip_acted_on_ineligible_and_old_jobs(session):
    keep = add_job(session, "Backend Engineer", score=70)
    unknown = add_job(session, "Python Developer", company="Beta", score=65, eligible=None)
    add_job(session, "Data Engineer", company="Gamma", score=90, eligible=False)
    acted = add_job(session, "Platform Engineer", company="Delta", score=95)
    tracker.set_status(session, oid(session), acted.id, "applied", now=NOW)
    add_job(session, "AI Engineer", company="Old", score=99, created_at=NOW - timedelta(days=40))
    add_job(session, "Unscored", analyzed=False)
    ids = [c.job.id for c in tracker.candidates(session, oid(session), since=NOW - timedelta(days=7))]
    assert ids == [keep.id, unknown.id]


def test_feedback_from_the_database_reorders_candidates(session):
    disliked = add_job(session, "Senior Backend Engineer", company="Acme", score=50)
    tracker.set_status(session, oid(session), disliked.id, "not_interested", now=NOW)
    similar_job = add_job(session, "Backend Engineer", company="Acme", score=80)
    other = add_job(session, "ML Engineer", company="Zeta", score=72, skills=["pytorch", "llm"])
    cands = tracker.candidates(session, oid(session))
    assert [c.job.id for c in cands] == [other.id, similar_job.id]
    penalized = cands[1]
    assert penalized.base == 80 and penalized.points == 60
    assert "Same company as a job you marked not for you" in penalized.adjustment.reasons


def test_pipeline_stats(session):
    a = add_job(session, score=80)
    add_job(session, "Data Engineer", score=40, eligible=None)
    add_job(session, "Ops", score=90, eligible=False)
    tracker.set_status(session, oid(session), a.id, "applied", now=NOW)
    tracker.mark_notified(session, oid(session), a.id, NOW)
    stats = tracker.pipeline_stats(session, oid(session), min_points=60, now=NOW)
    assert stats["jobs"] == 3 and stats["analyzed"] == 3
    assert stats["eligibility"] == {"eligible": 1, "unknown": 1, "ineligible": 1}
    assert stats["strong"] == 1 and stats["notified"] == 1
    assert stats["statuses"] == {"applied": 1}
