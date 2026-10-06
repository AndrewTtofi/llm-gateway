"""Chat completions ↔ OpenAI Responses API (ADR 0014)."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Registry
from app.providers.base import ProviderError, UnsupportedRequest
from app.providers.openai_responses import StreamTranslator, from_responses, to_responses
from tests.conftest import UPSTREAM

URL = f"{UPSTREAM}/responses"
MSGS = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "type": "function",
        "function": {"name": "get", "description": "d", "parameters": {"type": "object"}},
    }
]

RESPONSE: dict[str, Any] = {
    "id": "resp_1",
    "object": "response",
    "created_at": 1,
    "model": "tiny",
    "status": "completed",
    "output": [
        {"type": "reasoning", "summary": []},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello"}],
        },
    ],
    "usage": {
        "input_tokens": 30,
        "output_tokens": 5,
        "total_tokens": 35,
        "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 3},
    },
}


@pytest.fixture
def responses_model(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"api": "responses"}})


def events(*evts: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in evts).encode()


# --- request ------------------------------------------------------------------


def test_request_translation() -> None:
    body = to_responses(
        {
            "messages": [
                {"role": "system", "content": "Be brief."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look"},
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get", "arguments": '{"q":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "42"},
            ],
            "tools": TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "get"}},
            "max_completion_tokens": 100,
            "reasoning_effort": "high",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True},
            },
            "user": "u",
            "stop": ["END"],  # no Responses equivalent
        },
        "gpt-x",
        reasoning_mode="pro",
    )
    assert body["model"] == "gpt-x" and body["store"] is False  # never stored at OpenAI
    assert body["input"] == [
        {"role": "system", "content": "Be brief."},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Look"},
                {"type": "input_image", "image_url": "https://x/y.png", "detail": "auto"},
            ],
        },
        {"type": "function_call", "call_id": "call_1", "name": "get", "arguments": '{"q":1}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "42"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "get",
            "parameters": {"type": "object"},
            "strict": False,
            "description": "d",
        }
    ]
    assert body["tool_choice"] == {"type": "function", "name": "get"}
    assert body["max_output_tokens"] == 100
    assert body["reasoning"] == {"effort": "high", "mode": "pro"}
    assert body["text"] == {
        "format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}, "strict": True}
    }
    assert body["safety_identifier"] == "u" and "stop" not in body


def test_untranslatable_requests() -> None:
    with pytest.raises(UnsupportedRequest):
        to_responses({"messages": MSGS, "n": 2}, "m")
    with pytest.raises(UnsupportedRequest):
        to_responses({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]}, "m")


# --- response -----------------------------------------------------------------


def test_response_translation() -> None:
    out = from_responses("mock", RESPONSE)
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 5,
        "total_tokens": 35,
        "prompt_tokens_details": {"cached_tokens": 10, "cache_creation_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    calls = from_responses(
        "mock",
        {
            **RESPONSE,
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_9",
                    "name": "get",
                    "arguments": "{}",
                }
            ],
        },
    )["choices"][0]
    assert calls["finish_reason"] == "tool_calls"
    assert calls["message"]["tool_calls"][0]["id"] == "call_9"
    cut = from_responses(
        "mock",
        {**RESPONSE, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    )
    assert cut["choices"][0]["finish_reason"] == "length"
    with pytest.raises(ProviderError):
        from_responses("mock", {**RESPONSE, "status": "failed", "error": {"message": "x"}})


def test_stream_translation() -> None:
    t = StreamTranslator("mock", include_usage=True)
    feed = [
        {"type": "response.created", "response": {"id": "resp_1", "model": "tiny"}},
        {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "delta": "Hi "},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "get",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 1,
            "delta": '{"q":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 1,
            "delta": "1}",
        },
        {"type": "response.completed", "response": RESPONSE},
    ]
    chunks = [c for e in feed for c in t.feed(e)]
    assert t.completed
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[1]["choices"][0]["delta"] == {"content": "Hi "}
    assert chunks[2]["choices"][0]["delta"]["tool_calls"][0] == {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "get", "arguments": ""},
    }
    args = "".join(
        c["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] for c in chunks[3:5]
    )
    assert json.loads(args) == {"q": 1}
    assert chunks[5]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[6]["usage"]["prompt_tokens"] == 30 and chunks[6]["choices"] == []
    with pytest.raises(ProviderError):
        StreamTranslator("mock", False).feed({"type": "error", "message": "boom"})


# --- through the gateway --------------------------------------------------------


@respx.mock
def test_chat_request_goes_to_the_responses_endpoint(
    client: TestClient, responses_model: None
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "local", "messages": MSGS, "tools": TOOLS, "max_tokens": 9},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello"
    sent = json.loads(route.calls.last.request.content)
    assert sent["store"] is False and sent["max_output_tokens"] == 9
    assert sent["tools"][0]["name"] == "get"  # tools work again for these models


@respx.mock
def test_streaming_through_the_responses_api(client: TestClient, responses_model: None) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=events(
                {"type": "response.created", "response": {"id": "resp_1", "model": "tiny"}},
                {
                    "type": "response.output_text.delta",
                    "item_id": "m1",
                    "output_index": 0,
                    "delta": "Hel",
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "m1",
                    "output_index": 0,
                    "delta": "lo",
                },
                {"type": "response.completed", "response": RESPONSE},
            ),
        )
    )
    body = {
        "model": "local",
        "messages": MSGS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        lines = [line for line in resp.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert (
        "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"])
        == "Hello"
    )
    assert any(c.get("usage", {}).get("prompt_tokens") == 30 for c in chunks)


@respx.mock
def test_a_cut_responses_stream_is_an_in_band_error(
    client: TestClient, responses_model: None
) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=events(
                {"type": "response.created", "response": {"id": "r", "model": "tiny"}},
                {
                    "type": "response.output_text.delta",
                    "item_id": "m1",
                    "output_index": 0,
                    "delta": "Hel",
                },
            ),
        )
    )
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "local", "messages": MSGS, "stream": True}
    ) as resp:
        text = "".join(resp.iter_text())
    assert '"error"' in text and "[DONE]" not in text  # no end marker for a truncated answer


@respx.mock
def test_failed_response_falls_back(client: TestClient, responses_model: None) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={**RESPONSE, "status": "failed", "error": {"message": "x"}}
        )
    )
    resp = client.post("/v1/chat/completions", json={"model": "mock-then-ok", "messages": MSGS})
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
