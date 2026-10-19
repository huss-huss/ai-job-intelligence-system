"""Editing your profile from Telegram: every command, validation, and rescoring of recent jobs."""

from datetime import timedelta

import pytest
from app import tracker, users
from app.models import UserJobScore
from app.telegram import bot, profile_cmds
from sqlalchemy import select

from tests.tracker_fixtures import NOW, OWNER, add_job, add_member, message_update, owner, settings

FRIEND = "555001"


def say(session, fake_tg, text, chat_id=FRIEND):
    assert bot.handle_update(session, fake_tg.client(), settings(), message_update(text, chat_id=chat_id), now=NOW)
    return fake_tg.sent()[-1]["text"]


@pytest.fixture
def friend(session):
    return add_member(session, FRIEND)


def profile(session, user):
    session.refresh(user)
    return users.profile_model(user)


def test_profile_summary(session, fake_tg, friend):
    text = say(session, fake_tg, "/profile")
    assert text.startswith("<b>Your profile</b>") and "python 3" in text and "Country: Ethiopia" in text
    assert "Employment: full_time 1, contract 0.6" in text


def test_skills_add_change_and_remove(session, fake_tg, friend):
    assert say(session, fake_tg, "/skills add Rust 3").startswith("Skill <b>rust</b> set to weight 3.")
    assert profile(session, friend).skills["rust"] == 3
    say(session, fake_tg, "/skills add ci/cd")
    assert profile(session, friend).skills["ci/cd"] == 2  # default weight
    say(session, fake_tg, "/skills add rust 1")
    assert profile(session, friend).skills["rust"] == 1
    assert say(session, fake_tg, "/skills remove rust").startswith("Skill <b>rust</b> removed.")
    assert "rust" not in profile(session, friend).skills


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/skills", "Usage: /skills add"),
        ("/skills add python 4", "The weight is 1"),
        ("/skills add python 0", "The weight is 1"),
        ("/skills add <script>", "A skill is 1-40 characters"),
        ("/skills add " + "x" * 41, "A skill is 1-40 characters"),
        ("/skills remove cobol", "isn't one of your skills"),
        ("/titles add", "Usage: /titles"),
        ("/titles remove astronaut", "isn't one of your target titles"),
        ("/salary lots", "Usage: /salary"),
        ("/salary 99999999", "Usage: /salary"),
        ("/salary 2000000", "between 1 and 1,000,000"),
        ("/country", "Usage: /country"),
        ("/country 1234", "Usage: /country"),
        ("/seniority wizard", "Usage: /seniority"),
        ("/seniority", "Usage: /seniority"),
        ("/employment gig", "Usage: /employment"),
        ("/employment contract:2", "Usage: /employment"),
    ],
)
def test_bad_input_is_rejected_and_nothing_changes(session, fake_tg, friend, text, expected):
    before = profile(session, friend).model_dump()
    assert expected in say(session, fake_tg, text)
    assert profile(session, friend).model_dump() == before


def test_titles(session, fake_tg, friend):
    say(session, fake_tg, "/titles add Platform   Engineer")
    assert "platform engineer" in profile(session, friend).target_titles
    assert "already a target title" in say(session, fake_tg, "/titles add platform engineer")
    say(session, fake_tg, "/titles remove platform engineer")
    assert "platform engineer" not in profile(session, friend).target_titles


def test_keeping_at_least_one_title(session, fake_tg, friend):
    for title in list(profile(session, friend).target_titles)[:-1]:
        say(session, fake_tg, f"/titles remove {title}")
    last = profile(session, friend).target_titles[0]
    assert "Keep at least one" in say(session, fake_tg, f"/titles remove {last}")


@pytest.mark.parametrize(
    ("text", "value"), [("/salary 45000", 45000), ("/salary $45,000", 45000), ("/salary 45k", 45000)]
)
def test_salary(session, fake_tg, friend, text, value):
    say(session, fake_tg, text)
    assert profile(session, friend).min_salary_usd == value


def test_salary_can_be_cleared(session, fake_tg, friend):
    assert say(session, fake_tg, "/salary none").startswith("Minimum salary cleared.")
    assert profile(session, friend).min_salary_usd is None


def test_country_updates_eligibility_terms(session, fake_tg, friend):
    text = say(session, fake_tg, "/country kenya ke")
    p = profile(session, friend).location
    assert p.country == "Kenya" and p.country_code == "ke"
    assert {"worldwide", "anywhere", "global", "kenya", "africa", "emea"} <= set(p.eligible_terms)
    assert "ethiopia" not in p.eligible_terms and "Country set to <b>Kenya</b>" in text
    say(session, fake_tg, "/country Germany")
    p = profile(session, friend).location
    assert "europe" in p.eligible_terms and "africa" not in p.eligible_terms and "kenya" not in p.eligible_terms
    assert "mena" in p.eligible_terms  # a custom term from the original profile is kept


def test_seniority_and_employment(session, fake_tg, friend):
    say(session, fake_tg, "/seniority senior, lead")
    assert profile(session, friend).seniority == ["senior", "lead"]
    say(session, fake_tg, "/employment full-time,freelance:0.5,part_time")
    assert profile(session, friend).employment_types == {"full_time": 1.0, "contract": 0.5, "part_time": 1.0}


def test_an_edit_rescores_your_recent_jobs_and_changes_your_ranking(session, fake_tg, friend):
    rust = add_job(session, "Senior Rust Engineer", company="R", description="Rust systems work. Remote worldwide.")
    py = add_job(session, "Senior Backend Engineer", company="P", description="Python and FastAPI. Remote worldwide.")
    old = add_job(session, "Senior Rust Developer", company="O", created_at=NOW - timedelta(days=40))

    def mine(user_id):
        session.expire_all()
        return {r.job_id: r for r in session.scalars(select(UserJobScore).where(UserJobScore.user_id == user_id))}

    say(session, fake_tg, "/titles add rust engineer")
    before = mine(friend.id)[rust.id].score
    text = say(session, fake_tg, "/skills add rust 3")
    assert "Rescored your 2 jobs from the last 30 days." in text
    rows = mine(friend.id)
    assert "rust" in rows[rust.id].match["matched_skills"] and rows[rust.id].score > before
    assert rows[old.id].match.get("matched_skills") == ["python", "fastapi"]  # outside the window: untouched
    recent = tracker.candidates(session, friend.id, since=NOW - timedelta(days=30))
    assert {c.job.id for c in recent} == {rust.id, py.id}
    # The owner's scores and profile didn't move.
    me = owner(session)
    assert "rust" not in users.profile_model(me).skills
    assert mine(me.id)[rust.id].match["matched_skills"] == ["python", "fastapi"]


def test_the_owner_edits_materialize_their_profile(session, fake_tg):
    me = owner(session)
    assert me.profile is None  # the deployment profile until edited
    deployment = users.profile_model(me)
    say(session, fake_tg, "/salary 50000", chat_id=OWNER)
    session.refresh(me)
    assert me.profile["min_salary_usd"] == 50000 and me.profile["skills"] == deployment.skills


def test_regions_for():
    assert profile_cmds.regions_for("South Africa") == ["africa", "emea"]
    assert profile_cmds.regions_for("United Kingdom") == ["europe", "emea"]
    assert profile_cmds.regions_for("Brazil") == []
