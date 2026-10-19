"""AI in the bot: cover letters (/cover, the ✍️ button), resumes (/cv, file uploads) and AI spend in /stats.

Every model call goes through the budget guard (worker/budget.py). Generated letters are sent and
forgotten: nothing but the usage row (tokens, cost) is stored. Resumes are stored per user
(users.resume_text) and deleted with the user. Nothing here logs resume text, letters or chat ids.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from app import users
from app.models import JobRaw
from app.telegram.render import esc

if TYPE_CHECKING:
    from app.telegram.bot import Ctx

logger = logging.getLogger(__name__)

NO_AI = "AI features aren't set up on this deployment (no OPENAI_API_KEY)."
NO_RESUME = (
    "I need your resume first. Paste it after /cv, or upload it as a .txt, .md or .pdf file here. "
    "It's only used for your cover letters; /cv delete removes it."
)
REFUSED = {
    "daily": "You've reached today's limit of {cap} cover letters. It resets at midnight UTC.",
    "monthly": "This month's AI budget is used up, so cover letters are paused until the 1st (UTC).",
    "no_price": "Cover letters are paused: the configured model has no known price, so its cost can't be capped.",
}
FAILED = "I couldn't write that one just now (the AI service didn't answer). Try again in a few minutes."
RESUME_HELP = (
    "<b>Your resume</b> is used only to write your cover letters.\n"
    "• Paste it: <code>/cv</code> followed by the text\n"
    "• Or upload a .txt, .md or .pdf file to this chat\n"
    "• <code>/cv delete</code> removes it"
)


def _worker():
    users._worker_on_path()
    from worker.config import settings

    return settings


def ai_enabled() -> bool:
    s = _worker()
    return bool(s.openai_api_key or s.llm_base_url)


def make_llm_client():
    """The OpenAI SDK client (tests replace this with one on a mock transport)."""
    from worker.llm import make_client

    return make_client(_worker())


def caller(ctx: Ctx):
    """A budgeted LLMCaller whose ledger is the llm_usage table of this deployment."""
    _worker()
    from worker.ai import build_caller
    from worker.budget import SqlUsageStore, session_factory_for

    client = make_llm_client()
    if client is None:
        return None
    return build_caller(_worker(), store=SqlUsageStore(session_factory_for(ctx.session)), client=client)


def guard(ctx: Ctx):
    _worker()
    from worker.budget import BudgetGuard, SqlUsageStore, session_factory_for

    return BudgetGuard.from_settings(_worker(), SqlUsageStore(session_factory_for(ctx.session)))


# --------------------------------------------------------------------------- cover letters


def write_cover_letter(ctx: Ctx, job_id: int) -> str:
    """Generate and send a letter for one job. Returns a short label for logs and tests."""
    if not ai_enabled():
        ctx.send(NO_AI)
        return "cover:off"
    job = ctx.session.get(JobRaw, job_id)
    if job is None:
        ctx.send(f"No job #{job_id}.")
        return "cover:unknown-job"
    if not ctx.user.resume_text:
        ctx.send(NO_RESUME)
        return "cover:no-resume"
    from worker.cover_letter import JobInfo, generate

    resume = ctx.user.resume_text
    profile = users.profile_model(ctx.user)
    info = JobInfo(title=job.title, company=job.company, location=job.location, description=job.description)
    # The ledger writes in its own sessions; don't hold this one's locks (SQLite) during the call.
    ctx.session.commit()
    llm = caller(ctx)
    if llm is None:
        ctx.send(NO_AI)
        return "cover:off"
    result = generate(llm, resume=resume, job=info, profile=profile, user_id=ctx.user.id)
    if result.refused:
        ctx.send(REFUSED[result.refused].format(cap=llm.guard.max_cover_letters_per_day))
        return f"cover:refused-{result.refused}"
    if not result.text:
        ctx.send(FAILED)
        return "cover:failed"
    header = f"✍️ <b>Cover letter</b> · {esc(job.title, 80)}" + (f" · {esc(job.company, 50)}" if job.company else "")
    # Escaped plain text (no formatting inside), so it copies cleanly. Not stored anywhere.
    body = html.escape(result.text[:3500], quote=False)
    ctx.send(f"{header}\n\n{body}\n\n<i>Check every line before you send it; edit anything that isn't you.</i>")
    return "cover:sent"


def cmd_cover(ctx: Ctx, args: list[str]) -> None:
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        ctx.send("Usage: /cover &lt;job id&gt; (the #number on each job), or tap ✍️ Cover letter under a job.")
        return
    write_cover_letter(ctx, int(args[0].lstrip("#")))


# --------------------------------------------------------------------------- resumes


def _save_resume(ctx: Ctx, text: str) -> None:
    ctx.user.resume_text = text
    ctx.user.resume_updated_at = ctx.now
    ctx.session.commit()
    words = len(text.split())
    ctx.send(f"Saved your resume ({words} words). Tap ✍️ Cover letter under a job, or /cover &lt;job id&gt;.")


def cmd_cv(ctx: Ctx, args: list[str]) -> None:
    from worker.cover_letter import ResumeError, normalize_resume

    raw = ctx.text.split(None, 1)[1] if len(ctx.text.split(None, 1)) > 1 else ""
    if not raw.strip():
        stored = ctx.user.resume_text
        status = f"Stored: {len(stored.split())} words." if stored else "No resume stored yet."
        ctx.send(f"{RESUME_HELP}\n\n{status}")
        return
    if raw.strip().lower() == "delete":
        ctx.user.resume_text = None
        ctx.user.resume_updated_at = None
        ctx.session.commit()
        ctx.send("Your resume is deleted.")
        return
    try:
        text = normalize_resume(raw)
    except ResumeError as exc:
        ctx.send(esc(str(exc)))
        return
    _save_resume(ctx, text)


def handle_document(ctx: Ctx, document: dict) -> str:
    """A file sent to the bot: a resume upload (.txt, .md, .pdf)."""
    from worker.cover_letter import RESUME_FILE_MAX_BYTES, RESUME_SUFFIXES, ResumeError, resume_from_file

    name = str(document.get("file_name") or "")
    if not name.lower().endswith(RESUME_SUFFIXES) and document.get("mime_type") != "application/pdf":
        ctx.send("I can only read a resume from .txt, .md or .pdf files.")
        return "resume:bad-type"
    if int(document.get("file_size") or 0) > RESUME_FILE_MAX_BYTES:
        ctx.send("That file is over 2 MB; send a smaller one or paste the text after /cv.")
        return "resume:too-big"
    try:
        info = ctx.client.get_file(document["file_id"])
        data = ctx.client.download_file(info["file_path"], max_bytes=RESUME_FILE_MAX_BYTES)
        text = resume_from_file(name, data)
    except ResumeError as exc:
        ctx.send(esc(str(exc)))
        return "resume:unreadable"
    except Exception as exc:
        logger.warning("resume download failed: %s", type(exc).__name__)
        ctx.send("I couldn't download that file from Telegram. Try again, or paste the text after /cv.")
        return "resume:download-failed"
    _save_resume(ctx, text)
    return "resume:saved"


# --------------------------------------------------------------------------- /stats


def stats_lines(ctx: Ctx) -> list[str]:
    """AI usage for /stats: the owner sees the month's spend against the budget, a member only their own count."""
    if not ai_enabled():
        return []
    g = guard(ctx)
    if ctx.user.role == "owner":
        s = g.summary()
        split = " · ".join(f"{name} ${cost:.4f}" for name, cost in sorted(s.by_feature.items()))
        lines = [
            f"AI spend this month: ${s.month_cost:.4f} of ${g.monthly_budget_usd:.2f}"
            f" ({100 * s.month_cost / g.monthly_budget_usd if g.monthly_budget_usd else 0:.0f}%)"
            + (f" · {split}" if split else ""),
            f"AI today: {s.extractions_today}/{g.max_extractions_per_day} extractions"
            f" · {s.cover_letters_today} cover letters (all users)",
        ]
        return lines
    mine = g.summary(user_id=ctx.user.id)
    return [f"Cover letters today: {mine.cover_letters_today}/{g.max_cover_letters_per_day}"]
