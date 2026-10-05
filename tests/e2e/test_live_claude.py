"""Phase 2 DoD against real Claude: the same OpenAI client code works for fast/smart/local.

Costs a few cents (tiny prompts, max_tokens capped). Needs `make up` and
ANTHROPIC_API_KEY in .env. Run with `make test-live`.
"""

import json

import httpx
import openai
import pytest

GATEWAY = "http://localhost:8000"
pytestmark = [pytest.mark.live, pytest.mark.e2e]


@pytest.fixture(scope="module")
def sdk(gateway_key: str) -> openai.OpenAI:
    probe = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        timeout=60,
        headers={"Authorization": f"Bearer {gateway_key}"},
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
    )
    if "is not configured" in probe.text:
        pytest.skip("ANTHROPIC_API_KEY not set in .env")
    return openai.OpenAI(base_url=f"{GATEWAY}/v1", api_key=gateway_key)


@pytest.mark.parametrize("alias", ["fast", "smart", "local"])
def test_same_client_code_streams_from_every_alias(sdk: openai.OpenAI, alias: str) -> None:
    stream = sdk.chat.completions.create(
        model=alias,
        messages=[
            {"role": "system", "content": "Answer in one word."},
            {"role": "user", "content": "What color is the sky on a clear day?"},
        ],
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=200,
    )
    text, usage = "", None
    for c in stream:
        if c.choices:
            text += c.choices[0].delta.content or ""
        usage = c.usage or usage
    assert "blue" in text.lower()
    assert usage is not None and usage.prompt_tokens > 0


def test_tool_call_through_claude(sdk: openai.OpenAI) -> None:
    resp = sdk.chat.completions.create(
        model="fast",
        messages=[{"role": "user", "content": "What's the weather in Oslo? Use the tool."}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
        max_tokens=200,
    )
    choice = resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    call = choice.message.tool_calls[0]  # type: ignore[index]
    assert "oslo" in json.loads(call.function.arguments)["city"].lower()  # type: ignore[union-attr]
