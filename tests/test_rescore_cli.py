"""`firstfetch rescore`: every user's recent jobs are rescored with the current rules, so a rules change
(e.g. office days in the description) reaches jobs already stored."""

from app.models import UserJobScore
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_matching import NITRA
from tests.tracker_fixtures import add_job, add_member, owner
from worker import cli


def test_rescore_marks_stored_hybrid_jobs_ineligible(engine, session, capsys):
    me = owner(session)
    friend = add_member(session, "555001")
    job = add_job(session, "Senior Backend Engineer", description=f"Python and PostgreSQL. {NITRA}", eligible=True)
    session.commit()
    assert cli.main(["rescore", "--days", "36500", "--database-url", str(engine.url)]) == 0
    out = capsys.readouterr().out
    assert f"user #{me.id}: rescored" in out and f"user #{friend.id}: rescored" in out
    with Session(engine) as s:
        rows = s.scalars(select(UserJobScore).where(UserJobScore.job_id == job.id)).all()
        assert {r.user_id for r in rows} == {me.id, friend.id}
        assert all(r.match.get("eligible") is False for r in rows)
