"""Smoke tests for the OpenAI-compatible flagship providers (ADR 0012), against the real APIs.

One non-streaming and one streaming call per provider, plus a tool call where the model
supports it. Each provider is skipped while its key isn't set in .env, so this only costs
money (a few cents) for the providers you've configured. Needs `make up`. Run with
`make test-live`.

These check the parameter rules in config/models.yaml: a rejected parameter shows up as a
400 here instead of breaking a fallback chain in production.
"""

import json

import httpx
import pytest

GATEWAY = "http://localhost:8000"
pytestmark = [pytest.mark.live, pytest.mark.e2e]

TARGETS = [
    "openai/gpt-6-astra",
    "gemini/gemini-3.1-pro-preview",
    "xai/grok-4.7",
    "mistral/mistral-medium-3-5-26-04",
    "deepseek/deepseek-flash",
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def call(key: str, body: dict, stream: bool = False) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"}
    if not stream:
        return httpx.post(f"{GATEWAY}/v1/chat/completions", json=body, headers=headers, timeout=180)
    with httpx.stream(
        "POST",
        f"{GATEWAY}/v1/chat/completions",
        json={**body, "stream": True},
        headers=headers,
        timeout=180,
    ) as resp:
        resp.read()
        return resp


def skip_if_unconfigured(resp: httpx.Response, target: str) -> None:
    if resp.status_code == 503 and "all_providers_unavailable" in resp.text:
        pytest.skip(f"{target}: provider key not set in .env")


@pytest.mark.parametrize("target", TARGETS)
def test_chat_and_stream(gateway_key: str, target: str) -> None:
    # Parameters some providers reject, so the rules are exercised.
    body = {
        "model": target,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 400,
        "temperature": 0.2,
        "seed": 1,
        "user": "e2e",
        "reasoning_effort": "low",
    }
    resp = call(gateway_key, body)
    skip_if_unconfigured(resp, target)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-gateway-provider"] == target
    assert resp.json()["usage"]["completion_tokens"] > 0

    streamed = call(gateway_key, {**body, "stream_options": {"include_usage": True}}, stream=True)
    assert streamed.status_code == 200, streamed.text
    lines = [line for line in streamed.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert any(c.get("usage") for c in chunks), "no usage in stream (metering would estimate)"


@pytest.mark.parametrize("target", TARGETS)
def test_tool_call(gateway_key: str, target: str) -> None:
    body = {
        "model": target,
        "messages": [{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
        "tools": TOOLS,
        "max_tokens": 800,
    }
    resp = call(gateway_key, body)
    skip_if_unconfigured(resp, target)
    assert resp.status_code == 200, resp.text
    calls = resp.json()["choices"][0]["message"].get("tool_calls") or []
    assert calls and calls[0]["function"]["name"] == "get_weather"
    assert "city" in json.loads(calls[0]["function"]["arguments"])
