"""Per-provider parameter rules for OpenAI-compatible APIs (ADR 0012)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Registry, load_registry
from app.providers.base import UnsupportedRequest
from app.providers.openai_compat import rules_for, shape_request
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION

MSGS = [{"role": "user", "content": "hi"}]
IMAGE = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]


CFG: dict[str, Any] = {
    "params": {
        "allow": ["max_tokens", "temperature", "tools"],
        "rename": {"max_completion_tokens": "max_tokens", "seed": "random_seed"},
        "values": {"reasoning_effort": ["high"]},
    },
    "models": {
        "strict": {"params": {"drop": ["temperature"]}},
        "no-tools": {"tools": False},
        "text-only": {"vision": False},
    },
}


def test_rename_drop_allow_and_values() -> None:
    body = shape_request(
        "p",
        CFG,
        "strict",
        {
            "messages": MSGS,
            "max_completion_tokens": 50,
            "temperature": 0.3,
            "user": "someone",
            "reasoning_effort": "low",
            "stream_options": {"include_usage": True},
        },
    )
    assert body == {
        "messages": MSGS,
        "max_tokens": 50,  # renamed
        "stream_options": {"include_usage": True},  # essential, always kept
    }  # temperature dropped for this model; user not allowed; effort value not supported


def test_new_name_wins_when_the_client_sent_both() -> None:
    body = shape_request(
        "p", CFG, "x", {"messages": MSGS, "max_tokens": 10, "max_completion_tokens": 99}
    )
    assert body["max_tokens"] == 10


def test_supported_values_pass() -> None:
    cfg = {"params": {"values": {"reasoning_effort": ["high"]}}}
    assert (
        shape_request("p", cfg, "x", {"messages": MSGS, "reasoning_effort": "high"})[
            "reasoning_effort"
        ]
        == "high"
    )


def test_missing_capabilities_skip_the_target() -> None:
    with pytest.raises(UnsupportedRequest, match="tools"):
        shape_request("p", CFG, "no-tools", {"messages": MSGS, "tools": [{"type": "function"}]})
    shape_request("p", CFG, "no-tools", {"messages": MSGS})  # fine without tools
    with pytest.raises(UnsupportedRequest, match="images"):
        shape_request("p", CFG, "text-only", {"messages": IMAGE})


def test_no_rules_means_unchanged() -> None:
    req = {"messages": MSGS, "temperature": 1, "anything": True}
    assert shape_request("p", {}, "m", req) == req


@respx.mock
def test_a_model_without_tools_falls_back_instead_of_failing(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"tools": False}})
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    resp = client.post(
        "/v1/chat/completions", json={"model": "mock-then-ok", "messages": MSGS, "tools": tools}
    )
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert not route.called
    # without tools the same model serves
    resp = client.post("/v1/chat/completions", json={"model": "mock-then-ok", "messages": MSGS})
    assert resp.headers["x-gateway-provider"] == "mock/tiny"
    assert "tools" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_rules_apply_to_what_is_sent(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        registry.providers["mock"], "params", {"rename": {"max_tokens": "max_completion_tokens"}}
    )
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS, "max_tokens": 7})
    sent = json.loads(route.calls.last.request.content)
    assert sent["max_completion_tokens"] == 7 and "max_tokens" not in sent


def test_shipped_config_rules() -> None:
    """The real providers' rules, as researched (ADR 0012)."""
    providers = load_registry(Path("config")).providers
    astra = rules_for(providers["openai"], "gpt-6-astra")
    assert astra["tools"] is False and "temperature" in astra["drop"]
    assert astra["rename"] == {"max_tokens": "max_completion_tokens"}
    mistral = rules_for(providers["mistral"], "mistral-medium-3-5-26-04")
    assert "user" not in mistral["allow"] and "reasoning_effort" not in mistral["allow"]
    assert providers["mistral"]["stream_usage"] is False
    assert {"stop", "presence_penalty"} <= rules_for(providers["xai"], "grok-4.7")["drop"]
    for name in ("gemini", "xai", "mistral", "deepseek"):
        assert providers[name]["api_key_env"], name  # keys from env, never in config
