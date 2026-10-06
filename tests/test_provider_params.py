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
    assert body == {"messages": MSGS, "max_tokens": 50}  # renamed
    # temperature dropped for this model; user not allowed; effort value not supported;
    # stream_options is the adapter's decision (only when streaming), not the rules'


def test_shape_request_leaves_the_callers_request_alone() -> None:
    req = {"messages": MSGS, "max_completion_tokens": 5, "user": "u"}
    before = dict(req)
    shape_request("p", CFG, "strict", req)
    assert req == before


def test_rename_target_wins_when_the_client_sent_both() -> None:
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
    with pytest.raises(UnsupportedRequest, match="text only"):
        shape_request("p", CFG, "text-only", {"messages": IMAGE})
    pdf = [{"role": "user", "content": [{"type": "file", "file": {"file_data": "x"}}]}]
    with pytest.raises(UnsupportedRequest, match="text only"):
        shape_request("p", CFG, "text-only", {"messages": pdf})
    legacy = {"messages": MSGS, "functions": [{"name": "f"}]}
    with pytest.raises(UnsupportedRequest, match="tools"):
        shape_request("p", CFG, "no-tools", legacy)


def test_tool_fields_are_removed_for_a_model_without_tools() -> None:
    body = shape_request(
        "p", CFG, "no-tools", {"messages": MSGS, "tools": [], "tool_choice": "auto"}
    )
    assert "tools" not in body and "tool_choice" not in body


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
    providers = load_registry(Path(__file__).parent.parent / "config").providers
    astra = rules_for(providers["openai"], "gpt-6-astra")
    assert astra["api"] == "responses" and astra["tools"] is True and "temperature" in astra["drop"]
    assert astra["rename"] == {"max_tokens": "max_completion_tokens"}
    mistral = rules_for(providers["mistral"], "mistral-medium-3-5-26-04")
    assert "user" not in mistral["allow"] and "reasoning_effort" not in mistral["allow"]
    assert providers["mistral"]["stream_usage"] is False
    assert {"stop", "presence_penalty"} <= rules_for(providers["xai"], "grok-4.7")["drop"]
    for name in ("gemini", "xai", "mistral", "deepseek"):
        assert providers[name]["api_key_env"], name  # keys from env, never in config


@respx.mock
async def test_stream_sends_stream_options_only_when_the_provider_takes_them(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import providers
    from tests.test_chat import chunk, sse

    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse(chunk("hi"), "[DONE]"))
    )
    cfg = registry.providers["mock"]
    req = {"messages": MSGS, "stream_options": {"include_usage": True}}

    async def sent(cfg: dict[str, Any]) -> dict[str, Any]:
        adapter = providers.AdapterPool().get("mock", cfg)
        _ = [c async for c in adapter.stream("tiny", req)]
        out: dict[str, Any] = json.loads(route.calls.last.request.content)
        return out

    assert (await sent(cfg))["stream_options"] == {"include_usage": True}
    strict = {**cfg, "stream_usage": False, "params": {"allow": ["max_tokens"]}}
    assert "stream_options" not in await sent(strict)
    # an allowlist alone doesn't drop it: usage is still wanted
    assert "stream_options" in await sent({**cfg, "params": {"allow": ["max_tokens"]}})


@respx.mock
def test_non_stream_never_sends_stream_options(client: TestClient) -> None:
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    client.post(
        "/v1/chat/completions",
        json={"model": "local", "messages": MSGS, "stream_options": {"include_usage": True}},
    )
    assert "stream_options" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_images_fall_through_to_a_model_that_takes_them(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"vision": False}})
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    resp = client.post("/v1/chat/completions", json={"model": "mock-then-ok", "messages": IMAGE})
    assert resp.headers["x-gateway-provider"] == "chaos/ok" and not route.called


def test_every_target_unsupported_is_a_400(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"tools": False}})
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    resp = client.post(
        "/v1/chat/completions", json={"model": "local", "messages": MSGS, "tools": tools}
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unsupported_parameter"


async def test_unconfigured_provider_is_skipped_without_breaker_effects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routing import router

    monkeypatch.delenv("MOCK_API_KEY")
    for _ in range(8):  # more than the breaker threshold
        resp = client.post("/v1/chat/completions", json={"model": "mock-then-ok", "messages": MSGS})
        assert resp.headers["x-gateway-provider"] == "chaos/ok"
        assert resp.headers["x-gateway-attempts"] == "1"  # only the call that served
    assert str(await router.store.state("mock/tiny")) == "closed"


def test_unsupported_outranks_unconfigured(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    # chain: claude/old (unconfigured) → mock/tiny (can't do tools)
    monkeypatch.delenv("TEST_ANTHROPIC_KEY")
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"tools": False}})
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    resp = client.post(
        "/v1/chat/completions", json={"model": "claude-then-mock", "messages": MSGS, "tools": tools}
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "unsupported_parameter"
