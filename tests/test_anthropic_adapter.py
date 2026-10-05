"""Gateway → AnthropicAdapter → official SDK → in-memory Anthropic API.

The SDK is built on httpx2, which respx can't intercept, so the fake API is an
httpx2.MockTransport handed to the SDK client. Everything above the HTTP layer —
SDK request building, SSE parsing, typed errors — is the real code path.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Registry
from tests.conftest import ANTHROPIC_UPSTREAM

MSGS = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]

MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "old",
    "content": [{"type": "text", "text": "Hello!"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 3},
}


def events_sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = [
    {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 4},
    },
    {"type": "message_stop"},
]

Handler = Callable[[httpx2.Request], Any]


class FakeAnthropic:
    """Records requests; responds with whatever the test sets."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.respond: Handler = lambda req: httpx2.Response(200, json=MESSAGE)

    def __call__(self, req: httpx2.Request) -> Any:
        self.requests.append(req)
        return self.respond(req)

    @property
    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.requests[-1].content)
        return out


@pytest.fixture
def fake(registry: Registry) -> FakeAnthropic:
    api = FakeAnthropic()
    for model in ("old", "new"):
        adapter, _, _ = main.resolve_target(f"claude-{model}")
        adapter._client = anthropic.AsyncAnthropic(  # type: ignore[attr-defined]
            api_key="sk-ant-test",
            base_url=ANTHROPIC_UPSTREAM,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
        )
    return api


def post(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/chat/completions", json={"messages": MSGS, **body})


# --- non-streaming ---------------------------------------------------------


def test_chat_translates_both_ways(client: TestClient, fake: FakeAnthropic) -> None:
    resp = post(client, model="claude-old", temperature=0.5)
    assert resp.status_code == 200
    out = resp.json()
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hello!"}
    assert out["usage"]["total_tokens"] == 15
    assert resp.headers["x-gateway-provider"] == "claude/old"

    req = fake.requests[-1]
    assert req.url.path == "/v1/messages" and "beta" not in str(req.url)
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert fake.body["system"] == "Be brief."
    assert fake.body["max_tokens"] == 1000  # required by Anthropic; default from config
    assert fake.body["temperature"] == 0.5


def test_model_capabilities_from_config(client: TestClient, fake: FakeAnthropic) -> None:
    post(client, model="claude-new", temperature=0.5, reasoning_effort="high")
    req = fake.requests[-1]
    assert "temperature" not in fake.body  # this model rejects sampling params
    assert fake.body["output_config"] == {"effort": "high"}
    # refusal fallback: beta endpoint + header + `fallbacks: "default"`
    assert "beta=true" in str(req.url)
    assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert fake.body["fallbacks"] == "default"


def test_untranslatable_request_is_400(client: TestClient, fake: FakeAnthropic) -> None:
    resp = post(client, model="claude-old", n=3)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_parameter"
    assert not fake.requests  # never sent upstream


def test_missing_api_key_is_a_clear_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_KEY")
    resp = post(client, model="claude-old")
    assert resp.status_code == 502
    assert "TEST_ANTHROPIC_KEY is not set" in resp.json()["error"]["message"]


SECRET = "invalid x-api-key sk-ant-api03-SECRETSECRET for org 1234-abcd"  # noqa: S105 (fake)


@pytest.mark.parametrize(
    ("status", "etype", "client_status", "code"),
    [
        (400, "invalid_request_error", 400, "upstream_rejected"),
        (401, "authentication_error", 502, "upstream_error"),
        (402, "billing_error", 503, "upstream_quota_exhausted"),
        (404, "not_found_error", 502, "upstream_error"),
        (429, "rate_limit_error", 429, "upstream_rate_limited"),
        (529, "overloaded_error", 502, "upstream_error"),
    ],
)
def test_errors_mapped_and_kept_safe(
    client: TestClient,
    fake: FakeAnthropic,
    status: int,
    etype: str,
    client_status: int,
    code: str,
) -> None:
    fake.respond = lambda req: httpx2.Response(
        status,
        json={"type": "error", "error": {"type": etype, "message": SECRET}},
        headers={"retry-after": "3"},
    )
    resp = post(client, model="claude-old")
    assert resp.status_code == client_status
    assert resp.json()["error"]["code"] == code
    if status == 400:
        assert SECRET in resp.text  # describes the client's own request
    else:
        assert "SECRETSECRET" not in resp.text
    assert ("retry-after" in resp.headers) == (client_status == 429)


def test_timeout_is_504(client: TestClient, fake: FakeAnthropic) -> None:
    def slow(req: httpx2.Request) -> Any:
        raise httpx2.ReadTimeout("slow", request=req)

    fake.respond = slow
    assert post(client, model="claude-old").status_code == 504


# --- streaming -------------------------------------------------------------


def stream_lines(client: TestClient, **body: Any) -> tuple[int, list[str]]:
    with client.stream(
        "POST", "/v1/chat/completions", json={"messages": MSGS, "stream": True, **body}
    ) as resp:
        return resp.status_code, [ln for ln in resp.iter_lines() if ln]


def test_stream_translates_events_to_chunks(client: TestClient, fake: FakeAnthropic) -> None:
    fake.respond = lambda req: httpx2.Response(
        200, content=events_sse(*STREAM), headers={"content-type": "text/event-stream"}
    )
    status, lines = stream_lines(client, model="claude-old", stream_options={"include_usage": True})
    assert status == 200 and lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text == "Hello"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] == 4
    assert fake.body["stream"] is True


def test_stream_error_before_first_event_is_http_error(
    client: TestClient, fake: FakeAnthropic
) -> None:
    fake.respond = lambda req: httpx2.Response(
        529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    )
    status, _ = stream_lines(client, model="claude-old")
    assert status == 502


def test_stream_error_event_mid_stream_is_in_band(client: TestClient, fake: FakeAnthropic) -> None:
    err = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    fake.respond = lambda req: httpx2.Response(
        200, content=events_sse(*STREAM[:3], err), headers={"content-type": "text/event-stream"}
    )
    status, lines = stream_lines(client, model="claude-old")
    assert status == 200
    assert json.loads(lines[-1][6:])["error"]["message"] == "claude failed mid-stream"
    assert "data: [DONE]" not in lines


async def test_closing_stream_closes_the_sdk_response(fake: FakeAnthropic) -> None:
    closed = asyncio.Event()

    class Hanging(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield events_sse(*STREAM[:3])
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    fake.respond = lambda req: httpx2.Response(
        200, stream=Hanging(), headers={"content-type": "text/event-stream"}
    )
    adapter, model, _ = main.resolve_target("claude-old")
    gen = adapter.stream(model, {"messages": MSGS, "model": model})
    await anext(gen)
    await gen.aclose()  # what SSEResponse does on disconnect
    await asyncio.wait_for(closed.wait(), timeout=2)


# --- the Phase 2 promise: the same OpenAI client code works against Claude ----


async def test_openai_sdk_tool_call_streamed_from_claude(fake: FakeAnthropic) -> None:
    tool_events = [
        STREAM[0],
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_9",
                "name": "get_weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city": '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"Oslo"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]
    fake.respond = lambda req: httpx2.Response(
        200, content=events_sse(*tool_events), headers={"content-type": "text/event-stream"}
    )
    transport = httpx2.ASGITransport(app=main.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as http:
        sdk = openai.AsyncOpenAI(base_url="http://gw/v1", api_key="x", http_client=http)
        stream = await sdk.chat.completions.create(
            model="claude-new",
            messages=[{"role": "user", "content": "weather in Oslo?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
            stream=True,
        )
        args, finish = "", None
        async for c in stream:
            for tc in c.choices[0].delta.tool_calls or [] if c.choices else []:
                args += tc.function.arguments or "" if tc.function else ""
            if c.choices and c.choices[0].finish_reason:
                finish = c.choices[0].finish_reason
    assert json.loads(args) == {"city": "Oslo"}
    assert finish == "tool_calls"
    # claude-new rejects forced tool choice → sent as auto + instruction
    assert fake.body["tool_choice"] == {"type": "auto"}
    assert "`get_weather`" in fake.body["system"]
    assert fake.body["tools"][0]["eager_input_streaming"] is True


# --- transport failures while streaming (review findings) -------------------


class Scripted(httpx2.AsyncByteStream):
    """Sends `chunks`, then either raises `then` or (if None) hangs forever."""

    def __init__(self, chunks: list[bytes], then: Exception | None) -> None:
        self.chunks, self.then = chunks, then

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for c in self.chunks:
            yield c
        if self.then is not None:
            raise self.then
        await asyncio.Event().wait()


def scripted(fake: FakeAnthropic, chunks: list[bytes], then: Exception | None) -> None:
    fake.respond = lambda req: httpx2.Response(
        200, stream=Scripted(chunks, then), headers={"content-type": "text/event-stream"}
    )


def test_read_timeout_before_first_event_is_504(client: TestClient, fake: FakeAnthropic) -> None:
    scripted(fake, [], httpx2.ReadTimeout("silent"))
    status, _ = stream_lines(client, model="claude-old")
    assert status == 504


def test_no_first_event_within_first_token_budget_is_504(
    client: TestClient, fake: FakeAnthropic
) -> None:
    scripted(fake, [], None)  # headers arrive, then nothing (first_token = 0.5s in tests)
    status, _ = stream_lines(client, model="claude-old")
    assert status == 504


def test_connection_drop_mid_stream_is_in_band(client: TestClient, fake: FakeAnthropic) -> None:
    scripted(fake, [events_sse(*STREAM[:3])], httpx2.RemoteProtocolError("peer closed"))
    status, lines = stream_lines(client, model="claude-old")
    assert status == 200
    assert "connection failed" in json.loads(lines[-1][6:])["error"]["message"]
    assert "data: [DONE]" not in lines


def test_stream_cut_before_message_stop_is_not_complete(
    client: TestClient, fake: FakeAnthropic
) -> None:
    fake.respond = lambda req: httpx2.Response(
        200, content=events_sse(*STREAM[:4]), headers={"content-type": "text/event-stream"}
    )
    status, lines = stream_lines(client, model="claude-old")
    assert status == 200
    assert "ended early" in json.loads(lines[-1][6:])["error"]["message"]
    assert "data: [DONE]" not in lines
