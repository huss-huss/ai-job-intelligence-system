"""worker/budget.py: monthly and daily caps refuse before any call; usage is recorded per call."""

from datetime import UTC, date, datetime, timedelta

import pytest
from app import users
from app.models import LlmUsage
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.llm_fakes import FakeOpenAI
from tests.tracker_fixtures import add_member, owner
from worker.budget import (
    BudgetExceeded,
    BudgetGuard,
    JsonFileUsageStore,
    MemoryUsageStore,
    SqlUsageStore,
)
from worker.llm import LLMCaller, LLMConfig, Usage

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def guard(store=None, now=NOW, **kw) -> BudgetGuard:
    return BudgetGuard(store or MemoryUsageStore(), clock=lambda: now, **kw)


def spend(g: BudgetGuard, feature="extract", cost=0.001, user_id=None, **kw):
    r = g.reserve(feature, "gpt-6-luna", cost, user_id=user_id, **kw)
    g.record(r, model="gpt-6-luna", usage=Usage(100, 50, 10), cost_usd=cost)
    return r


def test_monthly_budget_refuses_when_spend_plus_estimate_would_exceed_it():
    g = guard(monthly_budget_usd=0.80)
    spend(g, cost=0.79)
    spend(g, cost=0.0099)  # 0.7999: still under
    with pytest.raises(BudgetExceeded) as exc:
        g.reserve("extract", "gpt-6-luna", 0.0002)
    assert exc.value.reason == "monthly" and "0.80" in str(exc.value)
    assert len(g.store.rows) == 2  # nothing was reserved for the refused call


def test_monthly_budget_resets_on_the_first_of_the_month():
    store = MemoryUsageStore()
    spend(guard(store, now=datetime(2026, 9, 30, 23, 0, tzinfo=UTC)), cost=0.80)
    with pytest.raises(BudgetExceeded):
        guard(store, now=datetime(2026, 9, 30, 23, 30, tzinfo=UTC)).reserve("extract", "m", 0.01)
    guard(store, now=datetime(2026, 10, 1, 0, 5, tzinfo=UTC)).reserve("extract", "m", 0.01)


def test_daily_extraction_cap_counts_all_users_and_resets_next_day():
    store = MemoryUsageStore()
    g = guard(store, max_extractions_per_day=3)
    for _ in range(3):
        spend(g)
    with pytest.raises(BudgetExceeded) as exc:
        g.reserve("extract", "gpt-6-luna", 0.0001)
    assert exc.value.reason == "daily" and "3/3" in str(exc.value)
    assert g.remaining_extractions() == 0
    g.reserve("eval", "gpt-6-luna", 0.0001, count_daily=False)  # the eval tool isn't capped per day
    tomorrow = guard(store, now=NOW + timedelta(days=1), max_extractions_per_day=3)
    assert tomorrow.remaining_extractions() == 3


def test_cover_letter_cap_is_per_user():
    g = guard(max_cover_letters_per_day=2)
    spend(g, "cover_letter", user_id=1)
    spend(g, "cover_letter", user_id=1)
    with pytest.raises(BudgetExceeded) as exc:
        g.reserve("cover_letter", "gpt-6-luna", 0.001, user_id=1)
    assert exc.value.reason == "daily"
    g.reserve("cover_letter", "gpt-6-luna", 0.001, user_id=2)
    assert g.summary(user_id=1).cover_letters_today == 2 and g.summary().cover_letters_today == 3


def test_reservation_holds_the_worst_case_until_recorded():
    g = guard(monthly_budget_usd=0.01)
    r = g.reserve("extract", "gpt-6-luna", 0.006)
    with pytest.raises(BudgetExceeded):  # a concurrent caller sees the reservation
        g.reserve("extract", "gpt-6-luna", 0.006)
    g.record(r, model="gpt-6-luna", usage=Usage(1000, 200, 80), cost_usd=0.0002)
    g.reserve("extract", "gpt-6-luna", 0.006)
    g.release(g.reserve("extract", "gpt-6-luna", 0.001))
    assert [row["status"] for row in g.store.rows] == ["recorded", "reserved"]


def test_sql_store_records_usage_including_reasoning_tokens(engine):
    factory = lambda: Session(engine)  # noqa: E731
    g = guard(SqlUsageStore(factory), max_cover_letters_per_day=1)
    with Session(engine) as s:
        member = add_member(s, "555001")
        member_id = member.id
    fake = FakeOpenAI(content="Dear team", usage={"prompt": 900, "completion_tokens": 300, "reasoning": 140})
    caller = LLMCaller(fake.client(), LLMConfig(model="gpt-6-luna"), g)
    caller.complete([{"role": "user", "content": "x"}], feature="cover_letter", max_output_tokens=1400,
                    user_id=member_id)  # fmt: skip
    with Session(engine) as s:
        (row,) = s.scalars(select(LlmUsage)).all()
        expected = ("cover_letter", "gpt-6-luna", member_id, date(2026, 10, 19))
        assert (row.feature, row.model, row.user_id, row.day) == expected
        assert (row.input_tokens, row.output_tokens, row.reasoning_tokens, row.status) == (900, 300, 140, "recorded")
        assert row.cost_usd == pytest.approx((900 * 0.10 + 300 * 0.50) / 1e6)
    with pytest.raises(BudgetExceeded):
        g.reserve("cover_letter", "gpt-6-luna", 0.001, user_id=member_id)
    s = g.summary()
    assert s.month_calls == 1 and s.by_feature == {"cover_letter": pytest.approx(0.00024)}


def test_removing_a_member_keeps_their_spend_without_the_link(engine):
    g = guard(SqlUsageStore(lambda: Session(engine)))
    with Session(engine) as s:
        owner(s)
        member = add_member(s, "555002")
        spend(g, "cover_letter", cost=0.3, user_id=member.id)
        users.remove_member(s, member.id)
        s.commit()
        (row,) = s.scalars(select(LlmUsage)).all()
        assert row.user_id is None and row.cost_usd == pytest.approx(0.3)
    assert g.summary().month_cost == pytest.approx(0.3)


def test_json_file_store_survives_restarts(tmp_path):
    path = tmp_path / "ledger" / "llm_usage.json"
    spend(guard(JsonFileUsageStore(path)), cost=0.5)
    again = guard(JsonFileUsageStore(path), monthly_budget_usd=0.8)
    assert again.summary().month_cost == pytest.approx(0.5)
    with pytest.raises(BudgetExceeded):
        again.reserve("extract", "gpt-6-luna", 0.31)
    path.write_text("not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        JsonFileUsageStore(path)
