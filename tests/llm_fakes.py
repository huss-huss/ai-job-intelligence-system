"""A fake OpenAI chat-completions API on httpx.MockTransport, used with the real openai SDK.

Nothing here touches the network. `FakeOpenAI.client()` is an `openai.OpenAI` whose HTTP client
answers from this class: queued responses first (`script`), else a reply built by `reply`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from openai import OpenAI

EXTRACTION = {
    "seniority": "senior",
    "skills": ["python", "fastapi"],
    "salary": {"min": None, "max": None, "currency": None, "period": None},
    "remote": True,
    "location_restriction": None,
    "worldwide": None,
    "employment_type": "full_time",
    "timezone_requirement": None,
    "years_experience_min": None,
    "role_family": "backend",
    "summary": "Backend role",
    "risk_flags": [],
}


def completion(content: str, *, model: str = "gpt-6-luna", prompt=500, completion_tokens=300, reasoning=120,
               finish_reason="stop") -> dict:  # fmt: skip
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_790_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def unsupported(param: str, kind: str = "parameter") -> httpx.Response:
    message = (
        f"Unsupported parameter: '{param}' is not supported with this model."
        if kind == "parameter"
        else f"Unsupported value: '{param}' does not support 0 with this model. Only the default (1) is supported."
    )
    body = {"error": {"message": message, "type": "invalid_request_error", "param": param,
                      "code": f"unsupported_{kind}"}}  # fmt: skip
    return httpx.Response(400, json=body)


def model_not_found(model: str) -> httpx.Response:
    body = {"error": {"message": f"The model `{model}` does not exist or you do not have access to it.",
                      "type": "invalid_request_error", "param": None, "code": "model_not_found"}}  # fmt: skip
    return httpx.Response(404, json=body)


@dataclass
class FakeOpenAI:
    content: str = field(default_factory=lambda: json.dumps(EXTRACTION))
    script: list = field(default_factory=list)  # httpx.Response or Exception, used first
    requests: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # overrides for completion()
    by_model: dict[str, str] = field(default_factory=dict)  # model -> content
    respond: Callable[[dict], str] | None = None  # request body -> content

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "llm.test", request.url
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        content = self.respond(body) if self.respond else self.by_model.get(body.get("model"), self.content)
        return httpx.Response(200, json=completion(content, model=body.get("model", "?"), **self.usage))

    def client(self) -> OpenAI:
        return OpenAI(
            api_key="sk-test",
            base_url="https://llm.test/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            max_retries=0,
        )


def ai_settings(**overrides):
    """Worker settings with AI on (a fake key) and the default caps."""
    from worker.config import Settings

    base = dict(
        openai_api_key="sk-test",
        openai_model="gpt-6-luna",
        llm_base_url="",
        llm_fallback_model="gpt-4.1-mini",
        llm_monthly_budget_usd=0.80,
        llm_max_extractions_per_day=30,
        llm_max_cover_letters_per_day=5,
        llm_reasoning_effort="",
        llm_prices_json="",
    )
    return Settings(**{**base, **overrides})
