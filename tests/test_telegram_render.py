"""Job cards, keyboards and chunking: HTML escaping and Telegram's size limits."""

import html
import re
from datetime import UTC

from app.models import JobAnalysis, JobRaw
from app.telegram.client import MAX_CALLBACK_DATA_BYTES, MAX_MESSAGE_CHARS
from app.telegram.render import (
    chunk,
    esc,
    job_card,
    job_keyboard,
    parse_callback,
    safe_url,
    top_reasons,
    zone,
)
from app.tracker import Adjustment, Candidate

TZ = zone("Africa/Addis_Ababa")


def _candidate(title="Backend Engineer", company="Acme", url="https://jobs.example.com/1", reasons=None, **extra):
    job = JobRaw(id=7, title=title, company=company, location="Remote", url=url)
    match = {"eligible": True, "reasons": reasons or ["Title matches target role 'backend'", "Skills: python"]}
    analysis = JobAnalysis(job_id=7, extracted_json={"match": match, **extra}, score=0.81)
    return Candidate(job, analysis, None, 81, Adjustment())


def _visible_text(message: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", message))


def test_card_escapes_html_in_every_field():
    c = _candidate(title="<script>alert(1)</script> & Co", company="A<b>", reasons=["Skills: c++ & <rust>"])
    card = job_card(c, TZ)
    assert "<script>" not in card and "&lt;script&gt;" in card
    assert "A&lt;b&gt;" in card and "c++ &amp; &lt;rust&gt;" in card
    # Only the tags we put there remain.
    assert set(re.findall(r"</?([a-z]+)", card)) <= {"b", "a", "i"}


def test_card_has_title_company_score_reasons_and_apply_link():
    card = job_card(_candidate(), TZ)
    text = _visible_text(card)
    assert "Backend Engineer" in text and "Acme · Remote" in text
    assert "Score 81" in text and "Eligible" in text
    assert "• Title matches target role 'backend'" in text and "• Skills: python" in text
    assert '<a href="https://jobs.example.com/1">Apply</a>' in card


def test_card_shows_top_two_reasons_only_and_skips_location():
    reasons = ["Title matches", "Skills: python", "Seniority fits (senior)", "Open to applicants in Ethiopia"]
    card = job_card(_candidate(reasons=reasons), TZ)
    assert "Seniority fits" not in card and "Open to applicants" not in card


def test_card_shows_hire_probability_and_feedback_when_present():
    c = _candidate(hire_probability=0.34)
    c.adjustment = Adjustment(-10, ["Similar to jobs you marked not for you (python)"])
    text = _visible_text(job_card(c, TZ))
    assert "Score 71 (81 − 10 feedback)" in text
    assert "Hire probability 34%" in text
    assert "↳ Similar to jobs you marked not for you (python)" in text


def test_card_with_huge_fields_stays_under_limit_and_never_cuts_an_entity():
    c = _candidate(title="&" * 5000, company="<" * 5000, reasons=["&" * 5000, ">" * 5000])
    card = job_card(c, TZ)
    assert len(card) <= MAX_MESSAGE_CHARS
    # Every & starts a complete entity: clipping happened before escaping.
    assert not re.search(r"&(?!(amp|lt|gt|quot|#x27);)", card)


def test_unsafe_urls_get_no_link_or_button():
    for url in ("javascript:alert(1)", "ftp://x/y", "/relative", ""):
        c = _candidate(url=url)
        assert "<a href" not in job_card(c, TZ)
        kb = job_keyboard(7, url, "new")
        assert all("url" not in b for row in kb["inline_keyboard"] for b in row)
    assert safe_url('https://x.example/?q="><b>') == 'https://x.example/?q="><b>'
    card = job_card(_candidate(url='https://x.example/?q="><b>'), TZ)
    assert 'href="https://x.example/?q=&quot;&gt;&lt;b&gt;"' in card


def test_status_line_after_an_action_in_local_time():
    from datetime import datetime

    card = job_card(_candidate(), TZ, status="applied", changed_at=datetime(2026, 10, 19, 6, 30, tzinfo=UTC))
    assert "<i>Status: ✅ Applied · 19 Oct 09:30</i>" in card  # UTC+3


def test_keyboard_buttons_mark_state_and_fit_callback_limits():
    kb = job_keyboard(123456789012, "https://jobs.example.com/1", "saved")
    first, second = kb["inline_keyboard"]
    assert [b["text"] for b in first] == ["✅ Applied", "⭐ Save ✓", "👎 Not for me"]
    assert [b.get("url") for b in second] == ["https://jobs.example.com/1", None]
    assert second[1]["text"] == "↩️ Undo" and second[1]["callback_data"] == "s:123456789012:new"
    for b in first:
        assert len(b["callback_data"].encode()) <= MAX_CALLBACK_DATA_BYTES
    assert len(job_keyboard(1, None, "new")["inline_keyboard"]) == 1


def test_parse_callback():
    assert parse_callback("s:12:applied") == (12, "applied")
    for bad in (None, "", "s:x:applied", "s:12:APPLIED", "s:12:applied:extra", "x:12:applied"):
        assert parse_callback(bad) is None


def test_chunk_packs_lines_under_the_limit_without_splitting_any():
    lines = [f"<b>{i}</b> " + esc("job & title " * 20) for i in range(200)]
    messages = chunk(lines)
    assert len(messages) > 1
    assert all(len(m) <= MAX_MESSAGE_CHARS for m in messages)
    assert "\n".join(messages).split("\n") == lines  # nothing lost, nothing reordered or cut


def test_chunk_drops_a_single_oversized_block():
    assert chunk(["ok", "x" * (MAX_MESSAGE_CHARS + 1), "fine"]) == ["ok\nfine"]


def test_top_reasons_puts_risks_first():
    reasons = ["Title matches", "Skills: python", "Risk: unpaid", "Restricted to USA"]
    assert top_reasons(reasons) == ["Risk: unpaid", "Title matches"]


def test_zone_falls_back_to_fixed_offset():
    assert zone("Not/AZone").utcoffset(None) is not None
