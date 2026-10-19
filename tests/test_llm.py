"""worker/llm.py: per-model parameters, the unsupported-parameter retry, the fallback model, usage and cost.

The real openai SDK talks to a fake API on httpx.MockTransport (tests/llm_fakes.py); no network.
"""

import httpx
import pytest

from tests.llm_fakes import FakeOpenAI, completion, model_not_found, unsupported
from worker import llm
from worker.budget import BudgetGuard, MemoryUsageStore
from worker.llm import LLMCaller, LLMCallError, LLMConfig, Price, caps_for, price_for, request_params

MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello " * 50}]


@pytest.fixture(autouse=True)
def _fresh_learning():
    llm.forget_learned()
    yield
    llm.forget_learned()


def make(fake: FakeOpenAI, model="gpt-6-luna", fallback="gpt-4.1-mini", **guard_kw):
    store = MemoryUsageStore()
    guard = BudgetGuard(store, **guard_kw)
    return LLMCaller(fake.client(), LLMConfig(model=model, fallback_model=fallback), guard), store


def test_price_table_is_dated_and_has_both_models():
    assert llm.PRICES_AS_OF == "2026-10-19" and "openai.com" in llm.PRICES_SOURCE
    assert price_for("gpt-6-luna") == Price(0.10, 0.50)
    assert price_for("gpt-4.1-mini") == Price(0.40, 1.60)
    assert price_for("gpt-4.1-mini-2025-04-14") == Price(0.40, 1.60)  # dated snapshot
    assert price_for("unknown-model") is None
    assert price_for("unknown-model", '{"unknown-model": [1, 2]}') == Price(1.0, 2.0)


def test_reasoning_model_gets_no_temperature_and_low_effort():
    params = request_params("gpt-6-luna", max_output_tokens=900, temperature=0)
    assert params == {"max_completion_tokens": 900, "extra_body": {"reasoning_effort": "low"}}
    assert request_params("gpt-6-luna", max_output_tokens=9, temperature=0, reasoning_effort="minimal")[
        "extra_body"
    ] == {"reasoning_effort": "minimal"}
    classic = request_params("gpt-4.1-mini", max_output_tokens=900, temperature=0)
    assert classic == {"max_completion_tokens": 900, "temperature": 0}
    assert caps_for("llama3.1").token_param == "max_tokens"  # local servers


def test_request_body_matches_capabilities():
    fake = FakeOpenAI(content="hi")
    caller, _ = make(fake)
    caller.complete(MESSAGES, feature="extract", max_output_tokens=900, temperature=0)
    body = fake.requests[0]
    assert body["model"] == "gpt-6-luna" and body["max_completion_tokens"] == 900
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body and "max_tokens" not in body


def test_unsupported_parameter_is_retried_once_without_it_and_remembered(caplog):
    fake = FakeOpenAI(content="ok", script=[unsupported("reasoning_effort")])
    caller, store = make(fake)
    result = caller.complete(MESSAGES, feature="extract", max_output_tokens=500, temperature=0)
    assert result.content == "ok" and result.retried_params == ["reasoning_effort"]
    assert "reasoning_effort" in fake.requests[0] and "reasoning_effort" not in fake.requests[1]
    assert "rejected reasoning_effort" in caplog.text
    # Remembered for the process: the next call doesn't send it at all.
    caller.complete(MESSAGES, feature="extract", max_output_tokens=500, temperature=0)
    assert len(fake.requests) == 3 and "reasoning_effort" not in fake.requests[2]
    assert [r["status"] for r in store.rows] == ["recorded", "recorded"]  # the 400 wasn't billed


def test_unsupported_temperature_value_is_dropped():
    fake = FakeOpenAI(content="ok", script=[unsupported("temperature", kind="value")])
    caller, _ = make(fake, model="gpt-4.1-mini")
    caller.complete(MESSAGES, feature="extract", max_output_tokens=500, temperature=0)
    assert fake.requests[0]["temperature"] == 0 and "temperature" not in fake.requests[1]
    assert "temperature" in llm.learned("gpt-4.1-mini")["dropped"]


def test_max_tokens_rejection_switches_to_max_completion_tokens():
    fake = FakeOpenAI(content="ok", script=[unsupported("max_tokens")])
    caller, _ = make(fake, model="some-new-model", fallback="")
    caller.config.prices_json = '{"some-new-model": [0.1, 0.5]}'
    caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    assert fake.requests[0]["max_tokens"] == 300 and fake.requests[1]["max_completion_tokens"] == 300
    assert llm.learned("some-new-model")["token_param"] == "max_completion_tokens"


def test_the_same_parameter_is_not_retried_twice():
    fake = FakeOpenAI(script=[unsupported("reasoning_effort"), unsupported("reasoning_effort")])
    caller, store = make(fake)
    with pytest.raises(LLMCallError):
        caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    assert len(fake.requests) == 2 and store.rows == []


def test_unavailable_model_switches_to_the_fallback_and_reserves_at_its_price():
    fake = FakeOpenAI(content="ok", script=[model_not_found("gpt-6-luna")])
    caller, store = make(fake)
    result = caller.complete(MESSAGES, feature="extract", max_output_tokens=500)
    assert [r["model"] for r in fake.requests] == ["gpt-6-luna", "gpt-4.1-mini"]
    assert result.model == "gpt-4.1-mini" and caller.model == "gpt-4.1-mini"
    assert "reasoning_effort" not in fake.requests[1]  # the fallback's own parameter set
    (row,) = store.rows
    assert row["model"] == "gpt-4.1-mini"
    assert row["cost_usd"] == pytest.approx(Price(0.40, 1.60).cost(500, 300))


def test_usage_and_cost_come_from_the_response_including_reasoning_tokens():
    fake = FakeOpenAI(content="ok", usage={"prompt": 1200, "completion_tokens": 400, "reasoning": 250})
    caller, store = make(fake)
    result = caller.complete(MESSAGES, feature="cover_letter", max_output_tokens=1400, user_id=7)
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.reasoning_tokens) == (1200, 400, 250)
    expected = (1200 * 0.10 + 400 * 0.50) / 1_000_000  # reasoning tokens are part of the billed output
    assert result.cost_usd == pytest.approx(expected)
    (row,) = store.rows
    expected_row = {"user_id": 7, "feature": "cover_letter", "model": "gpt-6-luna", "input_tokens": 1200,
                    "output_tokens": 400, "reasoning_tokens": 250, "status": "recorded"}  # fmt: skip
    assert {k: row[k] for k in expected_row} == expected_row
    assert row["cost_usd"] == pytest.approx(expected)


def test_estimate_is_a_worst_case_upper_bound():
    fake = FakeOpenAI(content="ok", usage={"prompt": 75, "completion_tokens": 900, "reasoning": 600})
    caller, _ = make(fake)
    worst = caller.estimate(MESSAGES, 900)
    result = caller.complete(MESSAGES, feature="extract", max_output_tokens=900)
    assert result.cost_usd <= worst


def test_timeout_keeps_the_estimate_and_4xx_releases_it():
    fake = FakeOpenAI(script=[httpx.ReadTimeout("slow"), httpx.Response(401, json={"error": {"message": "bad key"}})])
    caller, store = make(fake)
    with pytest.raises(LLMCallError):
        caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    with pytest.raises(LLMCallError):
        caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    (row,) = store.rows  # the 401 was released
    assert row["status"] == "uncertain" and row["cost_usd"] > 0


def test_local_server_is_free_and_unknown_openai_model_is_refused():
    fake = FakeOpenAI(content="ok")
    caller, store = make(fake, model="llama3.1", fallback="")
    with pytest.raises(llm.LLMRefused):
        caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    assert fake.requests == []
    caller.config.free = True
    caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    assert store.rows[0]["cost_usd"] == 0 and "max_tokens" in fake.requests[0]


def test_response_without_usage_records_the_estimate():
    body = completion("ok")
    del body["usage"]
    fake = FakeOpenAI(script=[httpx.Response(200, json=body)])
    caller, store = make(fake)
    caller.complete(MESSAGES, feature="extract", max_output_tokens=300)
    assert store.rows[0]["cost_usd"] == pytest.approx(caller.estimate(MESSAGES, 300))
