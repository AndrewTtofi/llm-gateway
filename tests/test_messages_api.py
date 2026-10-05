"""Inbound Anthropic Messages API (/v1/messages, ADR 0010).

The official `anthropic` SDK talks to the gateway in-process (httpx2 ASGITransport), so
its request building, x-api-key auth, SSE event parsing, message accumulation and typed
errors are all the real thing. Upstreams are mocked: an OpenAI-compatible provider with
respx, Anthropic with the FakeAnthropic transport.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from fastapi.testclient import TestClient

from app import main, messages_api
from app.config import Registry
from tests import test_anthropic_adapter
from tests.conftest import UPSTREAM
from tests.test_anthropic_adapter import FakeAnthropic
from tests.test_chat import COMPLETION, chunk, sent_body, sse

URL = f"{UPSTREAM}/chat/completions"
fake = test_anthropic_adapter.fake  # the in-memory Anthropic API fixture
MSGS: list[anthropic.types.MessageParam] = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def sdk(registry: Registry, api_key: str) -> AsyncIterator[anthropic.AsyncAnthropic]:
    transport = httpx2.ASGITransport(app=main.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as http:
        yield anthropic.AsyncAnthropic(
            base_url="http://gw", api_key=api_key, http_client=http, max_retries=0
        )


def tool_chunk(index: int, id_: str | None = None, name: str | None = None, args: str = "") -> Any:
    call: dict[str, Any] = {"index": index, "function": {"arguments": args}}
    if id_:
        call |= {"id": id_, "type": "function"}
        call["function"]["name"] = name
    c = chunk()
    c["choices"][0]["delta"] = {"tool_calls": [call]}
    return c


USAGE = {
    **chunk(),
    "choices": [],
    "usage": {
        "prompt_tokens": 30,
        "completion_tokens": 7,
        "total_tokens": 37,
        "prompt_tokens_details": {"cached_tokens": 10},
    },
}


# --- request translation (unit) --------------------------------------------


def test_request_translates_to_the_internal_format() -> None:
    out = messages_api.to_openai(
        {
            "model": "smart",
            "max_tokens": 100,
            "system": [
                {"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "…", "signature": "s"},
                        {"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": "toolu_1", "name": "get", "input": {"q": 1}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [
                                {"type": "text", "text": "sunny"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "AA",
                                    },
                                },
                            ],
                        },
                        {"type": "text", "text": "thanks"},
                    ],
                },
            ],
            "tools": [{"name": "get", "description": "d", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            "stop_sequences": ["END"],
            "temperature": 0.2,
            "top_k": 5,  # no OpenAI equivalent: dropped
            "metadata": {"user_id": "u1"},
            "output_config": {"effort": "high"},
        }
    )
    assert out["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "get", "arguments": '{"q": 1}'},
                }
            ],
        },
        # tool results first (they must follow the tool calls), then the rest of the turn
        {"role": "tool", "tool_call_id": "toolu_1", "content": "sunny"},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                {"type": "text", "text": "thanks"},
            ],
        },
    ]
    assert out["tools"] == [
        {
            "type": "function",
            "function": {"name": "get", "parameters": {"type": "object"}, "description": "d"},
        }
    ]
    assert out["tool_choice"] == "required" and out["parallel_tool_calls"] is False
    assert out["stop"] == ["END"] and out["temperature"] == 0.2 and "top_k" not in out
    assert out["user"] == "u1" and out["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"model": "m", "messages": MSGS}, "max_tokens: Field required"),
        (
            {
                "model": "m",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": [{"type": "document", "source": {}}]}],
            },
            "'document' is not supported",
        ),
        (
            {
                "model": "m",
                "max_tokens": 1,
                "messages": MSGS,
                "tools": [{"type": "web_search_20250305", "name": "w"}],
            },
            "web_search_20250305",
        ),
        ({"model": "m", "max_tokens": 1, "messages": "hi"}, "malformed request"),
    ],
)
def test_untranslatable_requests_are_rejected(body: dict[str, Any], error: str) -> None:
    with pytest.raises(messages_api.InboundError, match=error):
        messages_api.to_openai(body)


def test_response_translates_back_with_anthropic_usage() -> None:
    result = {
        **COMPLETION,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get", "arguments": '{"q":1}'},
                        }
                    ],
                },
            }
        ],
        "usage": USAGE["usage"],
    }
    msg = messages_api.from_openai(result)
    assert msg["type"] == "message" and msg["id"].startswith("msg_")
    assert msg["content"] == [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "call_1", "name": "get", "input": {"q": 1}},
    ]
    assert msg["stop_reason"] == "tool_use"
    # Anthropic's input_tokens excludes cache reads; OpenAI's prompt_tokens includes them.
    assert msg["usage"] == {
        "input_tokens": 20,
        "output_tokens": 7,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 0,
    }


# --- through the gateway, with the official SDK ---------------------------


@respx.mock
async def test_sdk_non_streaming(sdk: anthropic.AsyncAnthropic) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    msg = await sdk.messages.create(model="local", max_tokens=50, system="Be brief.", messages=MSGS)
    assert msg.content[0].type == "text" and msg.content[0].text == "hello"
    assert msg.stop_reason == "end_turn"
    assert msg.usage.input_tokens + msg.usage.output_tokens == 4
    sent = sent_body(route)  # the OpenAI-compatible upstream got OpenAI format
    assert sent["messages"][0] == {"role": "system", "content": "Be brief."}
    assert sent["max_tokens"] == 50 and sent["model"] == "tiny"


@respx.mock
async def test_sdk_streaming_text_and_tool_call(sdk: anthropic.AsyncAnthropic) -> None:
    finish = chunk(finish="tool_calls")
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=sse(
                chunk("Let me "),
                chunk("check."),
                tool_chunk(0, "call_1", "get_weather"),
                tool_chunk(0, args='{"city":'),
                tool_chunk(0, args=' "Paris"}'),
                finish,
                USAGE,
                "[DONE]",
            ),
        )
    )
    async with sdk.messages.stream(
        model="local",
        max_tokens=50,
        messages=MSGS,
        tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
    ) as stream:
        text = "".join([t async for t in stream.text_stream])
        final = await stream.get_final_message()
    assert text == "Let me check."
    assert [b.type for b in final.content] == ["text", "tool_use"]
    tool = final.content[1]
    assert (
        tool.type == "tool_use" and tool.name == "get_weather" and tool.input == {"city": "Paris"}
    )
    assert final.stop_reason == "tool_use"
    assert final.usage.output_tokens == 7 and final.usage.cache_read_input_tokens == 10


def test_stream_event_sequence(client: TestClient) -> None:
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, content=sse(chunk("hi"), chunk(finish="stop"), USAGE, "[DONE]")
            )
        )
        body = {"model": "local", "max_tokens": 5, "messages": MSGS, "stream": True}
        with client.stream("POST", "/v1/messages", json=body) as resp:
            events = [line[7:] for line in resp.iter_lines() if line.startswith("event: ")]
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert events == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


@respx.mock
async def test_mid_stream_failure_is_an_error_event(sdk: anthropic.AsyncAnthropic) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, content=sse(chunk("a"), {"error": {"message": "boom"}}))
    )
    with pytest.raises(anthropic.APIError):
        async with sdk.messages.stream(model="local", max_tokens=5, messages=MSGS) as stream:
            await stream.get_final_message()


async def test_sdk_raises_typed_errors(sdk: anthropic.AsyncAnthropic, registry: Registry) -> None:
    with pytest.raises(anthropic.NotFoundError, match="no-such-alias"):
        await sdk.messages.create(model="no-such-alias", max_tokens=5, messages=MSGS)
    with pytest.raises(anthropic.BadRequestError, match="document"):
        await sdk.messages.create(
            model="local",
            max_tokens=5,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "text", "media_type": "text/plain", "data": "x"},
                        }
                    ],
                }
            ],
        )
    bad = sdk.with_options(api_key="gw_" + "x" * 43)
    with pytest.raises(anthropic.AuthenticationError):
        await bad.messages.create(model="local", max_tokens=5, messages=MSGS)


def test_errors_use_the_anthropic_shape(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages", json={"model": "no-such-alias", "max_tokens": 5, "messages": MSGS}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "type": "error",
        "error": {"type": "not_found_error", "message": "The model 'no-such-alias' does not exist"},
    }
    assert (
        client.post("/v1/messages", content=b"{nope").json()["error"]["type"]
        == "invalid_request_error"
    )


def test_x_api_key_and_bearer_both_authenticate(registry: Registry, api_key: str) -> None:
    with TestClient(main.app) as c:
        body = {"model": "nope", "max_tokens": 5, "messages": MSGS}
        assert c.post("/v1/messages", json=body, headers={"x-api-key": api_key}).status_code == 404
        bearer = {"Authorization": f"Bearer {api_key}"}
        assert c.post("/v1/messages", json=body, headers=bearer).status_code == 404
        assert c.post("/v1/messages", json=body).status_code == 401


def test_claude_target_round_trip(client: TestClient, fake: FakeAnthropic) -> None:
    resp = client.post(
        "/v1/messages",
        json={"model": "claude-old", "max_tokens": 64, "system": "Be brief.", "messages": MSGS},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == [{"type": "text", "text": "Hello!"}]
    assert resp.json()["usage"]["input_tokens"] == 12
    assert fake.body["system"] == "Be brief." and fake.body["max_tokens"] == 64


@respx.mock
def test_falls_back_from_claude_to_another_provider(
    client: TestClient, fake: FakeAnthropic
) -> None:
    fake.respond = lambda req: httpx2.Response(
        529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    )
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    resp = client.post(
        "/v1/messages", json={"model": "claude-then-mock", "max_tokens": 5, "messages": MSGS}
    )
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "mock/tiny"
    assert resp.headers["x-gateway-fallback"] == "true"
    assert resp.json()["content"][0]["text"] == "hello"  # Anthropic format from an OpenAI provider


def test_count_tokens_estimates(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages/count_tokens",
        json={"model": "local", "messages": [{"role": "user", "content": "x" * 400}]},
    )
    assert resp.status_code == 200 and resp.json()["input_tokens"] >= 100


def test_messages_are_metered_like_chat(client: TestClient) -> None:
    from app import services

    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
        client.post("/v1/messages", json={"model": "local", "max_tokens": 5, "messages": MSGS})
    rows = services.usage.records  # type: ignore[attr-defined]
    assert rows and rows[-1].alias == "local" and rows[-1].status == 200


def test_tool_result_errors_are_marked() -> None:
    out = messages_api.to_openai(
        {
            "model": "m",
            "max_tokens": 1,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": "nope",
                            "is_error": True,
                        }
                    ],
                }
            ],
        }
    )
    assert out["messages"] == [
        {"role": "tool", "tool_call_id": "t", "content": "[tool error] nope"}
    ]


def test_stream_encoder_without_chunks_still_ends_cleanly() -> None:
    enc = messages_api.MessagesStream()
    names = [json.loads(e.split("data: ", 1)[1])["type"] for e in enc.end()]
    assert names == ["message_start", "message_delta", "message_stop"]


async def test_claude_stream_round_trip(sdk: anthropic.AsyncAnthropic, fake: FakeAnthropic) -> None:
    # Anthropic events → internal OpenAI chunks → Anthropic events again.
    fake.respond = lambda req: httpx2.Response(
        200,
        content=test_anthropic_adapter.events_sse(*test_anthropic_adapter.STREAM),
        headers={"content-type": "text/event-stream"},
    )
    async with sdk.messages.stream(model="claude-old", max_tokens=5, messages=MSGS) as stream:
        final = await stream.get_final_message()
    assert final.content[0].type == "text" and final.content[0].text == "Hello"
    assert final.stop_reason == "end_turn" and final.usage.output_tokens == 4
    assert fake.body["stream"] is True


def test_system_messages_inside_the_conversation_are_kept() -> None:
    out = messages_api.to_openai(
        {
            "model": "m",
            "max_tokens": 1,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": [{"type": "text", "text": "Now be terse."}]},
            ],
        }
    )
    assert out["messages"][1] == {"role": "system", "content": "Now be terse."}
