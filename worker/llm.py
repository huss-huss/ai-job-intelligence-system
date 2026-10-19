"""One budgeted way to call an OpenAI-compatible chat model.

Every LLM call in FirstFetch (job extraction, cover letters, the eval tool) goes through `LLMCaller`:

1. The cost is estimated (worst case: estimated input tokens plus the full output cap) and reserved
   with the budget guard (worker/budget.py) *before* the request. A refused reservation means no call.
2. Only parameters the model accepts are sent (`MODEL_CAPS`). If the API still answers 400 "unsupported
   parameter" (a new snapshot, a local server), the request is retried once without it, and the
   process remembers that for the model.
3. If the configured model doesn't exist on the key (404 / 403), the fallback model is used instead.
4. The real usage from the response (input, output, reasoning tokens) replaces the reservation.

Prices are a dated static table (`PRICES_AS_OF`); `LLM_PRICES_JSON` adds or overrides models.
The installed SDK (openai 1.55.3) has no Responses API and no `reasoning_effort` keyword, so calls use
chat.completions with `response_format` json_schema, and `reasoning_effort` goes in `extra_body`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from worker.budget import BudgetGuard, Reservation

logger = logging.getLogger(__name__)

# --- prices ------------------------------------------------------------------------------------------

PRICES_AS_OF = "2026-10-19"
PRICES_SOURCE = "https://developers.openai.com/api/docs/pricing"


@dataclass(frozen=True)
class Price:
    input_per_m: float  # USD per 1M input tokens
    output_per_m: float  # USD per 1M output tokens (reasoning tokens are billed as output)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


# Standard (non-batch) prices from PRICES_SOURCE on PRICES_AS_OF. Cached-input discounts are ignored,
# so recorded costs are an upper bound.
MODEL_PRICES: dict[str, Price] = {
    "gpt-6-luna": Price(0.10, 0.50),
    "gpt-4.1-mini": Price(0.40, 1.60),
}


# --- capabilities ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCaps:
    supports_temperature: bool = True
    token_param: str = "max_completion_tokens"  # or "max_tokens" (older/local OpenAI-compatible servers)
    reasoning_effort: str | None = None  # sent only to reasoning models


MODEL_CAPS: dict[str, ModelCaps] = {
    # Reasoning-style: fixed temperature, reasoning tokens count against max_completion_tokens.
    "gpt-6-luna": ModelCaps(supports_temperature=False, token_param="max_completion_tokens", reasoning_effort="low"),
    "gpt-4.1-mini": ModelCaps(supports_temperature=True, token_param="max_completion_tokens"),
}
# A model not in the table (e.g. llama3.1 on Ollama): the classic parameter set.
DEFAULT_CAPS = ModelCaps(supports_temperature=True, token_param="max_tokens")

TOKEN_PARAMS = ("max_completion_tokens", "max_tokens")


def _lookup(table: dict, model: str):
    """Exact name, else the longest table key the model starts with ("gpt-4.1-mini-2025-04-14")."""
    if model in table:
        return table[model]
    keys = sorted((k for k in table if model.startswith(k + "-")), key=len, reverse=True)
    return table[keys[0]] if keys else None


def caps_for(model: str) -> ModelCaps:
    return _lookup(MODEL_CAPS, model) or DEFAULT_CAPS


def price_for(model: str, overrides: str = "") -> Price | None:
    """Price from LLM_PRICES_JSON ('{"model": [input_per_m, output_per_m]}') or MODEL_PRICES."""
    if overrides:
        try:
            extra = {k: Price(float(v[0]), float(v[1])) for k, v in json.loads(overrides).items()}
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            logger.warning("LLM_PRICES_JSON is not valid JSON of {model: [input, output]}; ignored")
            extra = {}
        found = _lookup(extra, model)
        if found:
            return found
    return _lookup(MODEL_PRICES, model)


# Learned at runtime: model -> parameters the API rejected (and the token parameter it wants instead).
_learned: dict[str, dict[str, Any]] = {}
_learned_lock = threading.Lock()


def learned(model: str) -> dict[str, Any]:
    with _learned_lock:
        return dict(_learned.get(model, {}))


def forget_learned() -> None:
    """Tests only."""
    with _learned_lock:
        _learned.clear()


def _learn(model: str, **facts: Any) -> None:
    with _learned_lock:
        entry = _learned.setdefault(model, {"dropped": set()})
        dropped = facts.pop("dropped", None)
        if dropped:
            entry["dropped"] = set(entry["dropped"]) | {dropped}
        entry.update(facts)


def request_params(
    model: str, *, max_output_tokens: int, temperature: float | None, reasoning_effort: str = ""
) -> dict:
    """The keyword arguments for chat.completions.create that this model accepts."""
    caps = caps_for(model)
    facts = learned(model)
    dropped: set[str] = set(facts.get("dropped", set()))
    token_param = facts.get("token_param") or caps.token_param
    params: dict[str, Any] = {token_param: max_output_tokens}
    if temperature is not None and caps.supports_temperature and "temperature" not in dropped:
        params["temperature"] = temperature
    effort = reasoning_effort or caps.reasoning_effort
    if effort and caps.reasoning_effort is not None and "reasoning_effort" not in dropped:
        # openai 1.55.3 has no reasoning_effort keyword; extra_body adds it to the JSON body as-is.
        params["extra_body"] = {"reasoning_effort": effort}
    return params


_PARAM_IN_MESSAGE = re.compile(r"""(?:parameter|value)s?:?\s*['"`]?([a-z_]+)['"`]?""", re.IGNORECASE)


def unsupported_param(exc: Exception) -> str | None:
    """The parameter a 400 rejected ("Unsupported parameter: 'temperature' ..."), else None."""
    if getattr(exc, "status_code", None) != 400:
        return None
    body = getattr(exc, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    err = err if isinstance(err, dict) else {}
    message = str(err.get("message") or getattr(exc, "message", "") or exc)
    code = str(err.get("code") or "")
    if "unsupported" not in (code + message).lower() and "not supported" not in message.lower():
        return None
    param = err.get("param")
    if not param:
        m = _PARAM_IN_MESSAGE.search(message)
        param = m.group(1) if m else None
    return param if param in ("temperature", "reasoning_effort", *TOKEN_PARAMS) else None


def _model_unavailable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 404 or (status in (400, 403) and "model" in text and ("not exist" in text or "access" in text))


# --- token estimates ---------------------------------------------------------------------------------

CHARS_PER_TOKEN = 3.0  # English averages ~4; 3 over-estimates on purpose (the guard reserves the worst case)
MESSAGE_OVERHEAD_TOKENS = 12


def estimate_tokens(*texts: str) -> int:
    return sum(math.ceil(len(t) / CHARS_PER_TOKEN) for t in texts if t) + MESSAGE_OVERHEAD_TOKENS * len(texts)


def estimate_input_tokens(messages: list[dict], response_format: dict | None = None) -> int:
    texts = [str(m.get("content") or "") for m in messages]
    if response_format:
        texts.append(json.dumps(response_format))
    return estimate_tokens(*texts)


# --- calls -------------------------------------------------------------------------------------------


class LLMRefused(Exception):
    """The budget guard refused the call; nothing was sent."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason  # "monthly" | "daily" | "no_price"


class LLMCallError(Exception):
    """The call was made (or attempted) and failed."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens, as billed
    reasoning_tokens: int = 0

    @classmethod
    def from_response(cls, resp) -> Usage | None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "completion_tokens_details", None)
        return cls(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0) if details is not None else 0,
        )


@dataclass
class LLMResult:
    content: str
    model: str
    usage: Usage
    cost_usd: float
    finish_reason: str | None = None
    retried_params: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    model: str
    fallback_model: str = ""
    reasoning_effort: str = ""  # "" = the table's default for the model
    prices_json: str = ""
    free: bool = False  # a local server (LLM_BASE_URL that isn't OpenAI): nothing is billed

    @classmethod
    def from_settings(cls, settings) -> LLMConfig:
        base = (settings.llm_base_url or "").strip()
        return cls(
            model=settings.openai_model,
            fallback_model=settings.llm_fallback_model,
            reasoning_effort=settings.llm_reasoning_effort,
            prices_json=settings.llm_prices_json,
            free=bool(base) and "api.openai.com" not in base,
        )


class LLMCaller:
    """Budget-checked chat.completions calls (see the module docstring). Thread-safe for concurrent calls."""

    def __init__(self, client, config: LLMConfig, guard: BudgetGuard) -> None:
        self.client = client
        self.config = config
        self.guard = guard
        self._model = config.model
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return self._model

    def price(self, model: str | None = None) -> Price | None:
        if self.config.free:
            return Price(0.0, 0.0)
        return price_for(model or self._model, self.config.prices_json)

    def estimate(self, messages: list[dict], max_output_tokens: int, response_format: dict | None = None) -> float:
        """Worst-case cost: estimated input plus the whole output cap."""
        price = self.price()
        if price is None:
            return math.inf
        return price.cost(estimate_input_tokens(messages, response_format), max_output_tokens)

    def complete(
        self,
        messages: list[dict],
        *,
        feature: str,
        max_output_tokens: int,
        user_id: int | None = None,
        temperature: float | None = None,
        response_format: dict | None = None,
        count_daily: bool = True,
    ) -> LLMResult:
        """Raises LLMRefused (nothing sent) or LLMCallError (the call failed)."""
        while True:
            model = self._model
            price = self.price(model)
            if price is None:
                raise LLMRefused("no_price", f"no price known for model {model!r}; add it to LLM_PRICES_JSON")
            estimate = price.cost(estimate_input_tokens(messages, response_format), max_output_tokens)
            # Raises BudgetExceeded: nothing is sent.
            reservation = self.guard.reserve(feature, model, estimate, user_id=user_id, count_daily=count_daily)
            try:
                resp, retried = self._create(messages, model, max_output_tokens, temperature, response_format)
            except _ModelUnavailable as exc:
                self.guard.release(reservation)
                fallback = self.config.fallback_model
                if fallback and fallback != model and model == self.config.model:
                    # The fallback has its own price, so the next pass reserves again with it.
                    logger.warning("model %s is not available on this key; switching to %s", model, fallback)
                    with self._lock:
                        self._model = fallback
                    continue
                raise LLMCallError(f"model {model} is not available: {exc.__cause__}") from exc.__cause__
            except LLMCallError as exc:
                # A 4xx is never billed; a timeout or dropped connection may have been, so its estimate stays.
                if getattr(exc.__cause__, "status_code", None) is not None:
                    self.guard.release(reservation)
                else:
                    self.guard.settle_unknown(reservation)
                raise
            return self._settle(reservation, resp, model, retried, estimate)

    def _settle(self, reservation: Reservation, resp, model: str, retried: list[str], estimate: float) -> LLMResult:
        usage = Usage.from_response(resp)
        price = self.price(model) or Price(0, 0)
        if usage is None:  # a server that reports no usage: keep the worst case
            usage, cost = Usage(), estimate
        else:
            cost = price.cost(usage.input_tokens, usage.output_tokens)
        self.guard.record(reservation, model=model, usage=usage, cost_usd=cost)
        choice = resp.choices[0]
        return LLMResult(
            content=choice.message.content or "",
            model=model,
            usage=usage,
            cost_usd=cost,
            finish_reason=getattr(choice, "finish_reason", None),
            retried_params=retried,
        )

    def _create(self, messages, model, max_output_tokens, temperature, response_format):
        retried: list[str] = []
        while True:
            params = request_params(
                model,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                reasoning_effort=self.config.reasoning_effort,
            )
            if response_format is not None:
                params["response_format"] = response_format
            try:
                return self.client.chat.completions.create(model=model, messages=messages, **params), retried
            except Exception as exc:
                param = unsupported_param(exc)
                sent = set(params) | set(params.get("extra_body") or {})
                if param and param in sent and param not in retried and len(retried) < 3:
                    retried.append(param)
                    if param in TOKEN_PARAMS:
                        other = next(p for p in TOKEN_PARAMS if p != param)
                        _learn(model, token_param=other)
                        logger.warning("%s rejected %s; retrying with %s (remembered)", model, param, other)
                    else:
                        _learn(model, dropped=param)
                        logger.warning("%s rejected %s; retrying without it (remembered)", model, param)
                    continue
                if _model_unavailable(exc):
                    raise _ModelUnavailable() from exc
                raise LLMCallError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc


class _ModelUnavailable(Exception):
    pass


def make_client(settings, *, timeout: float | None = None):
    """An OpenAI SDK client, or None when no key / base URL is configured or the SDK is missing."""
    if not settings.openai_api_key and not settings.llm_base_url:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed; AI features are off")
        return None
    # No SDK retries: each retry would be another billable request outside the reservation.
    return OpenAI(
        api_key=settings.openai_api_key or "local",
        base_url=settings.llm_base_url or None,
        timeout=timeout or settings.llm_timeout_seconds,
        max_retries=0,
    )
