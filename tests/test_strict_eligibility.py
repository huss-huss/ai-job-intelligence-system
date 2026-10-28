"""Strict eligibility: region limits written in the description, required languages and experience gaps
become blockers, and Telegram (digest, /top, /today) sends only jobs confirmed open with no blockers.

The region cases are the real ones that slipped through: Niuro ("must reside in South America, Central
America or Mexico") and Aloware ("Latam and Europe remote ONLY"), plus look-alikes that must stay open."""

from pathlib import Path

import pytest
from app import users
from app.telegram import bot

from tests.tracker_fixtures import NOW, add_job, message_update, owner, settings
from worker.hire import blockers, required_years
from worker.matching import description_region_restriction, location_eligibility
from worker.profile import load_profile
from worker.sources.base import JobPosting

ROOT = Path(__file__).resolve().parent.parent
TERMS = ["worldwide", "anywhere", "global", "ethiopia", "africa", "emea", "mena"]


@pytest.fixture
def profile():
    return load_profile(ROOT / "profile.example.yaml")


def job(description: str, location: str = "Remote", title: str = "Senior Python Engineer") -> JobPosting:
    return JobPosting(
        source="test", external_id="1", url="https://example.com/1", title=title, company="Acme",
        location=location, description=description, remote=True,
    )  # fmt: skip


# ----------------------------------------------------------------------- regions in the description


@pytest.mark.parametrize(
    "text,places",
    [
        ("Position is 100% remote, but candidates must reside in South America, Central America or Mexico.",
         "South America, Central America, Mexico"),
        ("**Role open and available for Latam and Europe remote ONLY**", "Latam, Europe"),
        ("Remote, EU only.", "EU"),
        ("Open to candidates based in the U.S. or Canada.", "U.S., Canada"),
        ("Senior Agentic AI Engineer (only from EU)", "EU"),
        ("Remote (LATAM only)", "LATAM"),
        ("We are hiring in the US and Canada.", "US, Canada"),
        ("Applicants must be located in the United Kingdom.", "United Kingdom"),
    ],
)  # fmt: skip
def test_region_limits_in_the_description(text, places):
    assert description_region_restriction(text, TERMS) == places


@pytest.mark.parametrize(
    "text",
    [
        "You must be based in EMEA.",
        "Open to candidates in Africa and Europe.",
        "This role is open to candidates worldwide.",
        "You must be available for US hours and overlap with EU time zones.",
        "Must be able to work in the US time zone.",
        "Join us! We are open to new ideas. Contact us only via the form.",
        "Our engineers are based in Europe and Asia, and we hire globally.",
        "We are not restricted to the US; anyone can apply.",
        "You don't need to be based in the US.",
        "We work with clients in the US and Europe.",
        "Fully remote. Python, FastAPI and PostgreSQL only.",
    ],
)
def test_lookalikes_stay_open(text):
    assert description_region_restriction(text, TERMS) is None


def test_region_limit_makes_a_remote_job_ineligible(profile):
    niuro = job("Strong Python. Position is 100% remote, but candidates must reside in South America or Mexico.")
    verdict, why = location_eligibility(niuro, profile)
    assert verdict is False and why == "Description limits applicants to South America, Mexico"


def test_a_profile_in_that_region_stays_eligible(profile):
    profile.location.eligible_terms = ["worldwide", "anywhere", "latam", "mexico", "latin america"]
    text = "Role open and available for Latam and Europe remote ONLY."
    assert description_region_restriction(text, profile.location.eligible_terms) is None


# ----------------------------------------------------------------------- experience and languages


@pytest.mark.parametrize(
    "text,years",
    [
        ("10+ years of software engineering experience, including 5+ years building LLM-based solutions", 10),
        ("Minimum of 3 years' experience with Python", 3),
        ("3-5 yrs of hands-on Python experience", 3),
        ("Senior-level experience with 5+ years in software or AI engineering.", 5),
        ("4+ years of experience working with Python", 4),
        ("We have 12 years of experience delivering software. You need 2+ years of experience.", 2),
        ("Founded 10 years ago.", None),
        ("Our company has 15+ years in the market.", None),
        ("", None),
    ],
)
def test_required_years(text, years):
    assert required_years(text) == years


def test_blockers_for_language_and_a_big_experience_gap(profile):
    profile.years_experience = 3
    assert blockers(job("Required languages: English B2, Ukrainian Native"), profile) == ["Requires Ukrainian"]
    assert blockers(job("10+ years of software engineering experience."), profile) == [
        "Asks for 10+ years of experience (you have 3)"
    ]
    assert blockers(job("5+ years of Python experience."), profile) == []  # 2 more years: a stretch, not a block
    assert blockers(job("German is a plus."), profile) == []  # nice-to-have, not required
    profile.years_experience = None
    assert blockers(job("10+ years of experience."), profile) == []  # unknown experience: never blocks


def test_score_posting_records_blockers_in_the_match(profile):
    from worker.ai import HeuristicExtractor
    from worker.pipeline import score_posting

    profile.years_experience = 3
    posting = job("Fully remote, worldwide. Python, FastAPI. 10+ years of experience. Fluent German required.")
    scored = score_posting(posting, HeuristicExtractor().extract(posting), profile)
    assert scored.match.blockers == ["Requires German", "Asks for 10+ years of experience (you have 3)"]
    assert "Blocker: Requires German" in scored.match.reasons
    assert scored.match.to_dict()["blockers"] == scored.match.blockers


# ----------------------------------------------------------------------- strict Telegram


def _titles(fake_tg) -> list[str]:
    return [b["text"].split("\n")[0] for m, b in fake_tg.requests if m == "sendMessage" and b.get("reply_markup")]


def test_top_sends_only_confirmed_open_jobs_without_blockers(session, fake_tg):
    add_job(session, "Open Python Engineer", company="A", score=90)
    add_job(session, "Unknown Location Engineer", company="B", score=95, eligible=None)
    add_job(session, "Ukrainian Speaking Engineer", company="C", score=99,
            user_scores={owner(session).id: {"blockers": ["Requires Ukrainian"]}})  # fmt: skip
    bot.handle_update(session, fake_tg.client(), settings(), message_update("/top"), now=NOW)
    cards = _titles(fake_tg)
    assert len(cards) == 1 and "Open Python Engineer" in cards[0]
    fake_tg.requests.clear()
    bot.handle_update(session, fake_tg.client(), settings(digest_strict=False), message_update("/top"), now=NOW)
    assert len(_titles(fake_tg)) == 3  # strict off: everything not ineligible


def test_today_says_how_many_are_only_on_the_dashboard(session, fake_tg):
    add_job(session, "Open Python Engineer", company="A", score=90)
    add_job(session, "Unknown Location Engineer", company="B", score=95, eligible=None)
    bot.handle_update(session, fake_tg.client(), settings(), message_update("/today"), now=NOW)
    text = " ".join(b.get("text", "") for _, b in fake_tg.requests)
    assert "Open Python Engineer" in text and "Unknown Location Engineer" not in text
    assert "1 more with an unconfirmed location" in text


def test_experience_command_sets_shows_and_clears(session, fake_tg):
    me = owner(session)
    bot.handle_update(session, fake_tg.client(), settings(), message_update("/experience 3"), now=NOW)
    assert "set to <b>3</b>" in fake_tg.requests[-1][1]["text"]
    session.expire_all()
    assert users.profile_model(owner(session)).years_experience == 3
    bot.handle_update(session, fake_tg.client(), settings(), message_update("/profile"), now=NOW)
    assert "Experience: 3 years" in fake_tg.requests[-1][1]["text"]
    for bad in ("/experience", "/experience lots", "/experience 99"):
        bot.handle_update(session, fake_tg.client(), settings(), message_update(bad), now=NOW)
        assert "Usage: /experience" in fake_tg.requests[-1][1]["text"]
    bot.handle_update(session, fake_tg.client(), settings(), message_update("/experience none"), now=NOW)
    session.expire_all()
    assert users.profile_model(owner(session)).years_experience is None
    assert me.id == owner(session).id
