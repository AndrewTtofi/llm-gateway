"""Phase 1 DoD: the openai SDK streams a reply through the running gateway.

Needs `make up` and Ollama with the model behind the `local` alias. Free — no paid APIs.
Run with `make test-e2e`.
"""

import httpx
import openai
import pytest

GATEWAY = "http://localhost:8000"

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def stack_is_up() -> None:
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("gateway not running (make up)")


def test_sdk_streams_through_gateway() -> None:
    client = openai.OpenAI(base_url=f"{GATEWAY}/v1", api_key="unused-until-phase-4")
    raw = client.chat.completions.with_raw_response.create(
        model="local",
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=10,
    )
    assert raw.headers["x-gateway-provider"].startswith("ollama/")
    text, usage = "", None
    for c in raw.parse():
        if c.choices:
            text += c.choices[0].delta.content or ""
        usage = c.usage or usage
    assert "pong" in text.lower()
    assert usage is not None and usage.completion_tokens > 0


def test_sdk_non_streaming_through_gateway() -> None:
    client = openai.OpenAI(base_url=f"{GATEWAY}/v1", api_key="unused-until-phase-4")
    resp = client.chat.completions.create(
        model="local", messages=[{"role": "user", "content": "Say hi"}], max_tokens=10
    )
    assert resp.choices[0].message.content
