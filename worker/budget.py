"""Hard, local LLM budget, independent of the limit set on the OpenAI account.

Before every call `BudgetGuard.reserve` checks, in one locked transaction:

* month-to-date cost (UTC calendar month) + this call's worst-case estimate <= LLM_MONTHLY_BUDGET_USD,
* extractions today < LLM_MAX_EXTRACTIONS_PER_DAY (all users together),
* this user's cover letters today < LLM_MAX_COVER_LETTERS_PER_DAY,

and inserts a "reserved" row with the estimate, so two processes can't both spend the last cent.
After the call `record` replaces the estimate with the real usage; `release` deletes the row when the
API rejected the request (not billed); `settle_unknown` keeps the estimate when a timeout leaves it
unknown whether OpenAI billed it.

Stores: `SqlUsageStore` (the llm_usage table; Postgres takes an advisory lock), `JsonFileUsageStore`
(the CLI without a database) and `MemoryUsageStore` (tests).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x46465F4C4C4D  # "FF_LLM"


class BudgetExceeded(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason  # "monthly" | "daily"


@dataclass
class Reservation:
    id: Any
    feature: str
    model: str
    estimate: float
    user_id: int | None = None


@dataclass
class UsageSummary:
    month_cost: float = 0.0
    month_calls: int = 0
    by_feature: dict[str, float] = field(default_factory=dict)
    extractions_today: int = 0
    cover_letters_today: int = 0  # the asking user's, or everyone's when no user is given


class UsageTx(Protocol):
    def month_cost(self, month_start: date) -> float: ...
    def count(self, day: date, feature: str, user_id: int | None = None) -> int: ...
    def insert(self, row: dict) -> Any: ...


class UsageStore(Protocol):
    def transaction(self) -> Any: ...  # context manager yielding a UsageTx
    def update(self, row_id: Any, **values: Any) -> None: ...
    def delete(self, row_id: Any) -> None: ...
    def summary(self, month_start: date, day: date, user_id: int | None) -> UsageSummary: ...


def _month_start(day: date) -> date:
    return day.replace(day=1)


class BudgetGuard:
    def __init__(
        self,
        store: UsageStore,
        *,
        monthly_budget_usd: float = 0.80,
        max_extractions_per_day: int = 30,
        max_cover_letters_per_day: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.monthly_budget_usd = monthly_budget_usd
        self.max_extractions_per_day = max_extractions_per_day
        self.max_cover_letters_per_day = max_cover_letters_per_day
        self.clock = clock
        self._lock = threading.Lock()  # one process, several threads (concurrent extraction)

    @classmethod
    def from_settings(cls, settings, store: UsageStore, **kw) -> BudgetGuard:
        return cls(
            store,
            monthly_budget_usd=settings.llm_monthly_budget_usd,
            max_extractions_per_day=settings.llm_max_extractions_per_day,
            max_cover_letters_per_day=settings.llm_max_cover_letters_per_day,
            **kw,
        )

    def today(self) -> date:
        return self.clock().astimezone(UTC).date()

    def reserve(
        self, feature: str, model: str, estimate: float, *, user_id: int | None = None, count_daily: bool = True
    ) -> Reservation:
        """Check the caps and record the worst-case estimate. Raises BudgetExceeded."""
        day = self.today()
        with self._lock, self.store.transaction() as tx:
            spent = tx.month_cost(_month_start(day))
            if spent + estimate > self.monthly_budget_usd:
                raise BudgetExceeded(
                    "monthly",
                    f"monthly AI budget reached: ${spent:.4f} spent of ${self.monthly_budget_usd:.2f}"
                    f" (this call could cost up to ${estimate:.4f})",
                )
            if count_daily and feature == "extract":
                done = tx.count(day, "extract")
                if done >= self.max_extractions_per_day:
                    raise BudgetExceeded(
                        "daily", f"daily extraction cap reached ({done}/{self.max_extractions_per_day})"
                    )
            if count_daily and feature == "cover_letter":
                done = tx.count(day, "cover_letter", user_id)
                if done >= self.max_cover_letters_per_day:
                    raise BudgetExceeded(
                        "daily", f"daily cover-letter cap reached ({done}/{self.max_cover_letters_per_day})"
                    )
            row_id = tx.insert(
                {
                    "day": day,
                    "created_at": self.clock(),
                    "user_id": user_id,
                    "feature": feature,
                    "model": model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_usd": estimate,
                    "status": "reserved",
                }
            )
        return Reservation(row_id, feature, model, estimate, user_id)

    def remaining_extractions(self) -> int:
        s = self.summary()
        return max(0, self.max_extractions_per_day - s.extractions_today)

    def record(self, reservation: Reservation, *, model: str, usage, cost_usd: float) -> None:
        with self._lock:
            self.store.update(
                reservation.id,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cost_usd=cost_usd,
                status="recorded",
            )

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self.store.delete(reservation.id)

    def settle_unknown(self, reservation: Reservation) -> None:
        with self._lock:
            self.store.update(reservation.id, status="uncertain")

    def summary(self, user_id: int | None = None) -> UsageSummary:
        day = self.today()
        return self.store.summary(_month_start(day), day, user_id)


# --- stores --------------------------------------------------------------------------------------------


class _ListTx:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def month_cost(self, month_start: date) -> float:
        return sum(r["cost_usd"] for r in self.rows if r["day"] >= month_start)

    def count(self, day: date, feature: str, user_id: int | None = None) -> int:
        return sum(
            1
            for r in self.rows
            if r["day"] == day and r["feature"] == feature and (user_id is None or r["user_id"] == user_id)
        )

    def insert(self, row: dict) -> int:
        row_id = max((r["id"] for r in self.rows), default=0) + 1
        self.rows.append({**row, "id": row_id})
        return row_id


def _summary_from_rows(rows: list[dict], month_start: date, day: date, user_id: int | None) -> UsageSummary:
    out = UsageSummary()
    for r in rows:
        if r["day"] < month_start:
            continue
        out.month_cost += r["cost_usd"]
        out.month_calls += 1
        out.by_feature[r["feature"]] = out.by_feature.get(r["feature"], 0.0) + r["cost_usd"]
        if r["day"] == day and r["feature"] == "extract":
            out.extractions_today += 1
        if r["day"] == day and r["feature"] == "cover_letter" and (user_id is None or r["user_id"] == user_id):
            out.cover_letters_today += 1
    return out


class MemoryUsageStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    @contextmanager
    def transaction(self) -> Iterator[_ListTx]:
        snapshot = [dict(r) for r in self.rows]
        try:
            yield _ListTx(self.rows)
        except BaseException:
            self.rows[:] = snapshot
            raise

    def _row(self, row_id):
        return next(r for r in self.rows if r["id"] == row_id)

    def update(self, row_id, **values) -> None:
        self._row(row_id).update(values)

    def delete(self, row_id) -> None:
        self.rows[:] = [r for r in self.rows if r["id"] != row_id]

    def summary(self, month_start: date, day: date, user_id: int | None) -> UsageSummary:
        return _summary_from_rows(self.rows, month_start, day, user_id)


class JsonFileUsageStore(MemoryUsageStore):
    """A small JSON ledger for `discover` runs without a database (default .firstfetch/llm_usage.json)."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            try:
                for r in json.loads(self.path.read_text()):
                    self.rows.append({**r, "day": date.fromisoformat(r["day"])})
            except (ValueError, KeyError, TypeError):
                # Never silently reset a ledger: refusing every call is the safe failure.
                raise RuntimeError(f"LLM usage ledger {self.path} is unreadable; fix or remove it") from None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{**r, "day": r["day"].isoformat(), "created_at": str(r.get("created_at") or "")} for r in self.rows]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(self.path)

    @contextmanager
    def transaction(self):
        with super().transaction() as tx:
            yield tx
        self._save()

    def update(self, row_id, **values) -> None:
        super().update(row_id, **values)
        self._save()

    def delete(self, row_id) -> None:
        super().delete(row_id)
        self._save()


class _SqlTx:
    def __init__(self, session) -> None:
        self.session = session

    def month_cost(self, month_start: date) -> float:
        from app.models import LlmUsage
        from sqlalchemy import func, select

        return float(
            self.session.execute(
                select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(LlmUsage.day >= month_start)
            ).scalar()
            or 0.0
        )

    def count(self, day: date, feature: str, user_id: int | None = None) -> int:
        from app.models import LlmUsage
        from sqlalchemy import func, select

        stmt = select(func.count()).select_from(LlmUsage).where(LlmUsage.day == day, LlmUsage.feature == feature)
        if user_id is not None:
            stmt = stmt.where(LlmUsage.user_id == user_id)
        return int(self.session.execute(stmt).scalar() or 0)

    def insert(self, row: dict) -> int:
        from app.models import LlmUsage

        obj = LlmUsage(**row)
        self.session.add(obj)
        self.session.flush()
        return obj.id


class SqlUsageStore:
    """The llm_usage table. Each operation runs in its own short session and commits at once, so a
    reservation is visible to other processes before the call is made, whatever the caller's transaction."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    @contextmanager
    def transaction(self):
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                from sqlalchemy import text

                # Serializes check + insert across processes (cron run, webhook, CLI); released at commit.
                session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            try:
                yield _SqlTx(session)
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def update(self, row_id, **values) -> None:
        from app.models import LlmUsage
        from sqlalchemy import update

        with self.session_factory() as session:
            session.execute(update(LlmUsage).where(LlmUsage.id == row_id).values(**values))
            session.commit()

    def delete(self, row_id) -> None:
        from app.models import LlmUsage
        from sqlalchemy import delete

        with self.session_factory() as session:
            session.execute(delete(LlmUsage).where(LlmUsage.id == row_id))
            session.commit()

    def summary(self, month_start: date, day: date, user_id: int | None) -> UsageSummary:
        from app.models import LlmUsage
        from sqlalchemy import select

        with self.session_factory() as session:
            rows = session.execute(
                select(LlmUsage.day, LlmUsage.feature, LlmUsage.user_id, LlmUsage.cost_usd).where(
                    LlmUsage.day >= month_start
                )
            ).all()
        return _summary_from_rows(
            [{"day": r.day, "feature": r.feature, "user_id": r.user_id, "cost_usd": r.cost_usd} for r in rows],
            month_start,
            day,
            user_id,
        )


def session_factory_for(session) -> Callable:
    """New sessions on the same connection pool as `session` (the ledger commits on its own)."""
    from sqlalchemy.orm import Session

    bind = session.get_bind()
    return lambda: Session(bind)
