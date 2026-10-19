"""Shared fixtures for the tracker / Telegram tests: a migrated SQLite database, seed helpers and a
fake Telegram Bot API on httpx.MockTransport. Nothing here touches the network."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from app.models import JobAnalysis, JobRaw, Source, User, UserJobScore
from sqlalchemy.orm import Session

from tests.sqlite_support import alembic_config, sqlite_engine

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
OWNER = "424242"
TOKEN = "123456:TEST-token_value"
SECRET = "s3cret-Webhook_token"


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "template.db"
    engine = sqlite_engine(path)
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    engine.dispose()
    return path


@pytest.fixture
def engine(migrated_template, tmp_path):
    path = tmp_path / "test.db"
    shutil.copy(migrated_template, path)
    eng = sqlite_engine(path)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


def settings(**overrides):
    from app.telegram.config import TelegramSettings

    base = dict(
        bot_token=TOKEN,
        webhook_secret=SECRET,
        chat_id=OWNER,
        digest_size=5,
        digest_min_score=60,
        digest_max_age_days=7,
        digest_send_empty=True,
        timezone="Africa/Addis_Ababa",
        api_base="https://api.telegram.test",
    )
    return TelegramSettings(**{**base, **overrides})


_counter = {"n": 0}


def owner(s: Session) -> User:
    """The owner row (the migration creates it; its chat id follows TELEGRAM_CHAT_ID)."""
    from app import users

    user = users.ensure_owner(s, OWNER)
    s.commit()
    return user


def oid(s: Session) -> int:
    return owner(s).id


def add_member(s: Session, chat_id: str = "555001", name: str = "Friend", profile: dict | None = None) -> User:
    from app import users

    user = User(telegram_chat_id=chat_id, display_name=name, role="member", created_at=NOW)
    s.add(user)
    s.flush()
    user.profile = profile if profile is not None else users.profile_model(owner(s)).model_dump(mode="json")
    s.commit()
    return user


def _pct(value) -> int | None:
    if value is None:
        return None
    return round(value * 100) if value <= 1 else round(value)


def add_job(
    s: Session,
    title: str = "Senior Backend Engineer",
    *,
    company: str | None = "Acme",
    score: float = 80,
    eligible: bool | None = True,
    reasons: list[str] | None = None,
    skills: list[str] | None = None,
    created_at: datetime | None = None,
    url: str | None = None,
    location: str | None = "Remote (Worldwide)",
    extra: dict | None = None,
    analyzed: bool = True,
    user_scores: dict[int, dict] | None = None,
    description: str = "",
) -> JobRaw:
    """A stored, analyzed job. Every existing user gets the same score row (as if their profiles agreed)
    unless `user_scores` gives {user_id: {"score": .., "eligible": ..}} for some of them."""
    source = s.query(Source).first()
    if source is None:
        source = Source(name="Fixture", base_url="https://example.com")
        s.add(source)
        s.flush()
    _counter["n"] += 1
    n = _counter["n"]
    job = JobRaw(
        source_id=source.id,
        url=url or f"https://jobs.example.com/{n}",
        title=title,
        company=company,
        location=location,
        description=description,
        content_hash=f"hash-{n}",
        created_at=created_at or NOW - timedelta(hours=2),
    )
    s.add(job)
    s.flush()
    if analyzed:
        match = {
            "score": score,
            "eligible": eligible,
            "reasons": reasons
            if reasons is not None
            else ["Title matches target role 'backend'", "Skills: python, fastapi", "Open to applicants in Ethiopia"],
            "matched_skills": skills if skills is not None else ["python", "fastapi"],
        }
        s.add(
            JobAnalysis(
                job_id=job.id,
                extracted_json={"match": match, **(extra or {})},
                score=score / 100,
                skills=skills if skills is not None else ["python", "fastapi"],
            )
        )
        owner(s)
        for user in s.query(User).all():
            own = {**match, **((user_scores or {}).get(user.id) or {})}
            s.add(
                UserJobScore(
                    user_id=user.id,
                    job_id=job.id,
                    score=round(own["score"]),
                    eligible=own["eligible"],
                    rank=_pct((extra or {}).get("rank")),
                    hire_probability=_pct((extra or {}).get("hire_probability")),
                    match=own,
                    scored_at=NOW,
                )
            )
        s.flush()
    s.commit()
    return job


@dataclass
class FakeTelegram:
    """Records Bot API calls; `script` lets a test queue error responses for a method."""

    requests: list[tuple[str, dict]] = field(default_factory=list)
    script: dict[str, list[httpx.Response]] = field(default_factory=dict)
    fail_all: bool = False
    next_message_id: int = 1000
    files: dict[str, bytes] = field(default_factory=dict)  # file_id -> content, for getFile + download

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/file/bot{TOKEN}/"):
            file_id = request.url.path.rsplit("/", 1)[-1]
            self.requests.append(("downloadFile", {"file_id": file_id}))
            if file_id not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[file_id])
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        body = json.loads(request.content or b"{}")
        self.requests.append((method, body))
        if self.fail_all:
            raise httpx.ConnectError("network down")
        queued = self.script.get(method)
        if queued:
            return queued.pop(0)
        if method == "sendMessage":
            self.next_message_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        if method == "getFile":
            file_id = body["file_id"]
            size = len(self.files.get(file_id, b""))
            result = {"file_id": file_id, "file_size": size, "file_path": f"documents/{file_id}"}
            return httpx.Response(200, json={"ok": True, "result": result})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "FirstFetchTestBot"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(self, **kw):
        from app.telegram.client import TelegramClient

        sleeps = kw.pop("sleeps", None)
        return TelegramClient(
            TOKEN,
            api_base="https://api.telegram.test",
            transport=httpx.MockTransport(self.handler),
            min_interval=0,
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
            **kw,
        )

    def sent(self, method: str = "sendMessage") -> list[dict]:
        return [body for m, body in self.requests if m == method]

    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]


def tg_error(code: int, description: str, **parameters) -> httpx.Response:
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


@pytest.fixture
def fake_tg() -> FakeTelegram:
    return FakeTelegram()


def message_update(text: str, chat_id: str = OWNER, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": 10, "chat": {"id": int(chat_id), "type": "private"}, "text": text},
    }


def document_update(file_id: str, file_name: str, size: int, chat_id: str = OWNER, mime: str | None = None) -> dict:
    document = {"file_id": file_id, "file_unique_id": f"u-{file_id}", "file_name": file_name, "file_size": size}
    if mime:
        document["mime_type"] = mime
    return {
        "update_id": 3,
        "message": {"message_id": 11, "chat": {"id": int(chat_id), "type": "private"}, "document": document},
    }


def callback_update(data: str, chat_id: str = OWNER, message_id: int = 77) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": int(chat_id)},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": int(chat_id), "type": "private"}},
        },
    }


class AsyncSessionAdapter:
    """Enough of AsyncSession for the async routes (jobs, runs, sources) to run on a sync SQLite session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    async def execute(self, stmt, *args, **kwargs):
        return self.session.execute(stmt, *args, **kwargs)

    async def commit(self) -> None:
        self.session.commit()

    async def rollback(self) -> None:
        self.session.rollback()

    async def close(self) -> None:
        pass
