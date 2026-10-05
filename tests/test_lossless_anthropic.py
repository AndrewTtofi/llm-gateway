"""Prompt caching and extended thinking survive /v1/messages → Anthropic (ADR 0013), and
never leak to other providers or to OpenAI-format clients."""

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from fastapi.testclient import TestClient

from app import extensions, main
from app.config import Registry
from app.providers.anthropic_format import StreamTranslator, from_anthropic, to_anthropic
from tests import test_anthropic_adapter
from tests.conftest import UPSTREAM
from tests.test_anthropic_adapter import MESSAGE, FakeAnthropic, events_sse
from tests.test_chat import COMPLETION

fake = test_anthropic_adapter.fake  # the in-memory Anthropic API fixture
CC = {"type": "ephemeral"}
THINKING = {"type": "thinking", "thinking": "Let me think.", "signature": "sig-123"}

CACHED_REQUEST: dict[str, Any] = {
    "model": "claude-old",
    "max_tokens": 64,
    "system": [{"type": "text", "text": "Long instructions.", "cache_control": CC}],
    "thinking": {"type": "adaptive"},
    "tools": [{"name": "get", "input_schema": {"type": "object"}, "cache_control": CC}],
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Big document.", "cache_control": CC}],
        },
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "tool_use", "id": "toolu_1", "name": "get", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "42",
                    "cache_control": CC,
                }
            ],
        },
    ],
}


@pytest.fixture
async def sdk(registry: Registry, api_key: str) -> AsyncIterator[anthropic.AsyncAnthropic]:
    transport = httpx2.ASGITransport(app=main.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as http:
        yield anthropic.AsyncAnthropic(
            base_url="http://gw", api_key=api_key, http_client=http, max_retries=0
        )


def test_cache_breakpoints_and_thinking_reach_anthropic(
    client: TestClient, fake: FakeAnthropic
) -> None:
    assert client.post("/v1/messages", json=CACHED_REQUEST).status_code == 200
    sent = fake.body
    assert sent["system"] == [{"type": "text", "text": "Long instructions.", "cache_control": CC}]
    assert sent["tools"][0]["cache_control"] == CC
    assert sent["thinking"] == {"type": "adaptive"}
    user, assistant, tool_turn = sent["messages"]
    assert user["content"] == [{"type": "text", "text": "Big document.", "cache_control": CC}]
    assert assistant["content"][0] == THINKING  # first, signature intact
    assert assistant["content"][1]["type"] == "tool_use"
    assert tool_turn["content"][0]["cache_control"] == CC


def test_thinking_and_cache_usage_come_back(client: TestClient, fake: FakeAnthropic) -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 200,
    }
    fake.respond = lambda req: httpx2.Response(
        200, json={**MESSAGE, "content": [THINKING, {"type": "text", "text": "Hi"}], "usage": usage}
    )
    body = client.post(
        "/v1/messages", json={**CACHED_REQUEST, "thinking": {"type": "adaptive"}}
    ).json()
    assert body["content"][0] == THINKING and body["content"][1] == {"type": "text", "text": "Hi"}
    assert body["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 200,
    }


async def test_streamed_thinking_round_trips_through_the_sdk(
    sdk: anthropic.AsyncAnthropic, fake: FakeAnthropic
) -> None:
    stream = [
        {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Hmm, "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "ok."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-9"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "xyz"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": "Answer"},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]
    fake.respond = lambda req: httpx2.Response(
        200, content=events_sse(*stream), headers={"content-type": "text/event-stream"}
    )
    async with sdk.messages.stream(
        model="claude-old",
        max_tokens=64,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "q"}],
    ) as s:
        final = await s.get_final_message()
    kinds = [b.type for b in final.content]
    assert kinds == ["thinking", "redacted_thinking", "text"]
    first = final.content[0]
    assert first.type == "thinking" and first.thinking == "Hmm, ok." and first.signature == "sig-9"
    assert final.content[1].type == "redacted_thinking" and final.content[1].data == "xyz"
    assert final.content[2].type == "text" and final.content[2].text == "Answer"


def test_openai_clients_never_see_extension_fields(client: TestClient, fake: FakeAnthropic) -> None:
    fake.respond = lambda req: httpx2.Response(
        200, json={**MESSAGE, "content": [THINKING, {"type": "text", "text": "Hi"}]}
    )
    out = client.post(
        "/v1/chat/completions",
        json={"model": "claude-old", "messages": [{"role": "user", "content": "q"}]},
    ).json()
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hi"}


@respx.mock
def test_other_providers_never_see_extension_fields(client: TestClient) -> None:
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    assert client.post("/v1/messages", json={**CACHED_REQUEST, "model": "local"}).status_code == 200
    sent = json.dumps(json.loads(route.calls.last.request.content))
    assert "cache_control" not in sent and "thinking" not in sent


# --- units ------------------------------------------------------------------


def test_strip_request_leaves_the_original_alone() -> None:
    req = {
        "thinking": {"type": "adaptive"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "x", "cache_control": CC}]},
            {
                "role": "assistant",
                "content": "y",
                "thinking_blocks": [THINKING],
                "tool_calls": [{"id": "1", "cache_control": CC}],
            },
        ],
        "tools": [{"type": "function", "cache_control": CC}],
    }
    before = json.dumps(req, sort_keys=True)
    out = extensions.strip_request(req)
    assert "cache_control" not in json.dumps(out) and "thinking" not in json.dumps(out)
    assert json.dumps(req, sort_keys=True) == before
    plain = {"messages": [{"role": "user", "content": "hi"}]}
    assert extensions.strip_request(plain) is plain  # nothing to strip: no copy


def test_strip_chunk_drops_thinking_only_chunks() -> None:
    thinking = {
        "choices": [
            {
                "index": 0,
                "delta": {"thinking": {"index": 0, "thinking": "…"}},
                "finish_reason": None,
            }
        ]
    }
    assert extensions.strip_chunk(thinking) is None
    mixed = {
        "choices": [{"index": 0, "delta": {"thinking": {}, "content": "a"}, "finish_reason": None}]
    }
    assert extensions.strip_chunk(mixed) == {
        "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": None}]
    }


def test_to_anthropic_sends_plain_system_string_without_breakpoints() -> None:
    out = to_anthropic(
        {
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "hi"},
            ]
        },
        "m",
        {},
        100,
    )
    assert out["system"] == "Be brief."


def test_string_message_with_cache_control_becomes_a_block() -> None:
    out = to_anthropic(
        {"messages": [{"role": "user", "content": "doc", "cache_control": CC}]}, "m", {}, 100
    )
    assert out["messages"][0]["content"] == [{"type": "text", "text": "doc", "cache_control": CC}]


def test_from_anthropic_keeps_thinking_blocks() -> None:
    msg = from_anthropic({**MESSAGE, "content": [THINKING, {"type": "text", "text": "Hi"}]})[
        "choices"
    ][0]["message"]
    assert msg["thinking_blocks"] == [THINKING] and msg["content"] == "Hi"


def test_stream_translator_emits_thinking_deltas() -> None:
    t = StreamTranslator(include_usage=False)
    t.feed({"type": "message_start", "message": {**MESSAGE, "content": []}})
    start = t.feed(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        }
    )
    text = t.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "a"},
        }
    )
    sig = t.feed(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "s"},
        }
    )
    assert start[0]["choices"][0]["delta"]["thinking"] == {
        "index": 0,
        "start": {"type": "thinking"},
    }
    assert text[0]["choices"][0]["delta"]["thinking"] == {"index": 0, "thinking": "a"}
    assert sig[0]["choices"][0]["delta"]["thinking"] == {"index": 0, "signature": "s"}
