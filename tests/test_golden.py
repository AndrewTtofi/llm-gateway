"""Complete provider responses, replayed end to end (ADR 0026): what the client gets, and
what's billed. The fixtures in tests/fixtures/providers/ carry every documented field."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import respx
from fastapi.testclient import TestClient

from app import services
from app.config import Registry
from tests.conftest import UPSTREAM
from tests.test_anthropic_adapter import FakeAnthropic, fake  # noqa: F401 — fixture

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
ASK = [{"role": "user", "content": "What's the weather in Paris?"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def records() -> list[Any]:
    return services.usage.records  # type: ignore[attr-defined,no-any-return]


def sse_chunks(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(ln[6:]) for ln in lines if ln.startswith("data: {")]


def tool_args(chunks: list[dict[str, Any]]) -> str:
    return "".join(
        (tc.get("function") or {}).get("arguments") or ""
        for c in chunks
        for ch in c.get("choices") or []
        for tc in (ch.get("delta") or {}).get("tool_calls") or []
    )


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_message_with_thinking_and_a_tool_call(
    client: TestClient,
    fake: FakeAnthropic,  # noqa: F811
) -> None:
    fake.respond = lambda req: httpx2.Response(
        200, json=json.loads(fixture("anthropic_message_tool_use.json"))
    )
    r = client.post(
        "/v1/chat/completions", json={"model": "claude-old", "messages": ASK, "tools": TOOLS}
    )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris", "unit": "celsius"}
    assert "thinking_blocks" not in choice["message"]  # OpenAI clients never see them
    usage = r.json()["usage"]
    assert usage["prompt_tokens"] == 512 + 1024 + 2048  # input + cache writes + cache reads
    assert usage["prompt_tokens_details"]["cached_tokens"] == 2048
    assert records()[-1].prompt_tokens == 3584 and records()[-1].completion_tokens == 96


def test_anthropic_stream_with_thinking_and_a_tool_call(
    client: TestClient,
    fake: FakeAnthropic,  # noqa: F811
) -> None:
    fake.respond = lambda req: httpx2.Response(
        200,
        content=fixture("anthropic_stream_thinking_tool.sse").encode(),
        headers={"content-type": "text/event-stream"},
    )
    body = {"model": "claude-old", "messages": ASK, "tools": TOOLS, "stream": True}
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        lines = list(r.iter_lines())
    chunks = sse_chunks(lines)
    assert [ln for ln in lines if ln][-1] == "data: [DONE]"  # a complete answer
    assert json.loads(tool_args(chunks)) == {"city": "Paris", "unit": "celsius"}
    text = "".join(
        (ch.get("delta") or {}).get("content") or "" for c in chunks for ch in c["choices"]
    )
    assert text == "Let me check the weather."
    assert [ch["finish_reason"] for c in chunks for ch in c["choices"] if ch["finish_reason"]] == [
        "tool_calls"
    ]
    assert records()[-1].completion_tokens == 89 and not records()[-1].usage_estimated


def test_anthropic_stream_to_an_anthropic_client_keeps_thinking(
    client: TestClient,
    fake: FakeAnthropic,  # noqa: F811
) -> None:
    fake.respond = lambda req: httpx2.Response(
        200,
        content=fixture("anthropic_stream_thinking_tool.sse").encode(),
        headers={"content-type": "text/event-stream"},
    )
    body = {"model": "claude-old", "max_tokens": 1024, "messages": ASK, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as r:
        events = [json.loads(ln[6:]) for ln in r.iter_lines() if ln.startswith("data: ")]
    deltas = [e["delta"]["type"] for e in events if e["type"] == "content_block_delta"]
    assert "thinking_delta" in deltas and "signature_delta" in deltas
    assert "input_json_delta" in deltas and "text_delta" in deltas
    assert events[-1]["type"] == "message_stop"


# --- OpenAI chat completions -----------------------------------------------------------------


@respx.mock
def test_openai_tool_call_answer(registry: Registry, client: TestClient) -> None:
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=json.loads(fixture("openai_chat_tool.json")))
    )
    r = client.post(
        "/v1/chat/completions", json={"model": "local", "messages": ASK, "tools": TOOLS}
    )
    assert r.status_code == 200
    call = r.json()["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris", "unit": "celsius"}
    rec = records()[-1]
    assert (rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens) == (3072, 118, 2048)


@respx.mock
def test_openai_stream_with_a_tool_call(registry: Registry, client: TestClient) -> None:
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=fixture("openai_chat_stream_tool.sse").encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    body = {"model": "local", "messages": ASK, "tools": TOOLS, "stream": True}
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        lines = list(r.iter_lines())
    chunks = sse_chunks(lines)
    assert json.loads(tool_args(chunks)) == {"city": "Paris", "unit": "celsius"}
    assert all(c.get("choices") for c in chunks)  # the usage-only chunk wasn't asked for
    assert records()[-1].completion_tokens == 118 and records()[-1].cached_tokens == 2048


# --- OpenAI Responses API --------------------------------------------------------------------


@respx.mock
def test_responses_api_stream_with_hidden_reasoning_and_a_tool_call(
    registry: Registry, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"api": "responses"}})
    respx.post(f"{UPSTREAM}/responses").mock(
        return_value=httpx.Response(
            200,
            content=fixture("responses_stream_tool.sse").encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    body = {
        "model": "local",
        "messages": ASK,
        "tools": TOOLS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        lines = list(r.iter_lines())
    chunks = sse_chunks(lines)
    assert json.loads(tool_args(chunks)) == {"city": "Paris"}
    finishes = [ch["finish_reason"] for c in chunks for ch in c["choices"] if ch["finish_reason"]]
    assert finishes == ["tool_calls"]
    usage = next(c["usage"] for c in chunks if c.get("usage"))
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 180
    assert records()[-1].completion_tokens == 210  # reasoning billed as output
