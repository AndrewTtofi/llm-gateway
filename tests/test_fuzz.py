"""Property-based fuzzing of everything that parses untrusted input (ADR 0026).

Two properties:
- **The API never answers 500.** Any JSON body sent to /v1/chat/completions or
  /v1/messages, valid or not, gets a 2xx or a 4xx: a client mistake is never the
  gateway's crash.
- **Stream translators fail cleanly.** Any sequence of (possibly malformed) provider stream
  events either translates or raises ProviderError, the error the relay turns into an
  in-band error, never KeyError/TypeError/AttributeError, which would be a 500 or a dead
  stream.

Runs a bounded number of examples per test (HYPOTHESIS_PROFILE=ci in CI, `dev` locally).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import main
from app.config import Registry
from app.providers import anthropic_format, openai_responses
from app.providers.base import ProviderError, UnsupportedRequest
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION

settings.register_profile(
    "dev", max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "ci", max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
FIXTURE_OK = [HealthCheck.function_scoped_fixture]

# --- strategies ------------------------------------------------------------------------

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**40), 2**40),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
json_values = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
roles = st.sampled_from(["system", "developer", "user", "assistant", "tool", "bogus", ""])
part_types = st.sampled_from(
    [
        "text",
        "image_url",
        "image",
        "file",
        "input_audio",
        "tool_use",
        "tool_result",
        "thinking",
        "document",
        "bogus",
    ]
)
parts = st.fixed_dictionaries(
    {"type": part_types},
    optional={
        "text": st.one_of(st.text(max_size=30), json_values),
        "image_url": json_values,
        "source": json_values,
        "id": st.text(max_size=8),
        "name": st.text(max_size=8),
        "input": json_values,
        "content": json_values,
        "tool_use_id": st.text(max_size=8),
        "cache_control": json_values,
        "thinking": st.text(max_size=10),
        "signature": st.text(max_size=10),
    },
)
content = st.one_of(st.text(max_size=40), st.lists(parts, max_size=4), st.none(), json_values)
messages = st.lists(
    st.fixed_dictionaries(
        {"role": roles, "content": content},
        optional={
            "tool_calls": json_values,
            "tool_call_id": st.text(max_size=8),
            "name": st.text(max_size=8),
            "thinking_blocks": json_values,
        },
    ),
    max_size=5,
)
extras = st.dictionaries(
    st.sampled_from(
        [
            "max_tokens",
            "max_completion_tokens",
            "n",
            "stream",
            "temperature",
            "tools",
            "tool_choice",
            "system",
            "stop",
            "stop_sequences",
            "metadata",
            "thinking",
            "route",
            "user",
            "response_format",
            "stream_options",
            "top_k",
            "reasoning_effort",
        ]
    ),
    json_values,
    max_size=6,
)


def body(model: Any, msgs: Any, extra: dict[str, Any]) -> dict[str, Any]:
    return {**extra, "model": model, "messages": msgs}


models = st.one_of(st.just("local"), st.text(max_size=10), json_values)

# --- the API never answers 500 --------------------------------------------------------------


@pytest.fixture
def api(registry: Registry, auth: dict[str, str]) -> Any:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{UPSTREAM}/chat/completions").mock(
            return_value=httpx.Response(200, json=COMPLETION)
        )
        with TestClient(main.app, headers=auth, raise_server_exceptions=False) as c:
            yield c


@settings(suppress_health_check=FIXTURE_OK)
@given(model=models, msgs=st.one_of(messages, json_values), extra=extras)
def test_chat_completions_never_500(api: TestClient, model: Any, msgs: Any, extra: Any) -> None:
    r = api.post("/v1/chat/completions", json=body(model, msgs, extra))
    assert r.status_code < 500 or r.status_code in (502, 503, 504), r.text[:300]


@settings(suppress_health_check=FIXTURE_OK)
@given(model=models, msgs=st.one_of(messages, json_values), extra=extras)
def test_messages_never_500(api: TestClient, model: Any, msgs: Any, extra: Any) -> None:
    r = api.post("/v1/messages", json=body(model, msgs, extra))
    assert r.status_code < 500 or r.status_code in (502, 503, 504), r.text[:300]


@settings(suppress_health_check=FIXTURE_OK)
@given(raw=st.one_of(json_values, st.binary(max_size=60)))
def test_any_body_never_500(api: TestClient, raw: Any) -> None:
    data = raw if isinstance(raw, bytes) else json.dumps(raw).encode()
    for path in ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens"):
        r = api.post(path, content=data, headers={"content-type": "application/json"})
        assert r.status_code < 500, (path, r.text[:300])


# --- translators -----------------------------------------------------------------------------

ANTHROPIC_EVENTS = [
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
    "ping",
    "error",
]
anthropic_events = st.lists(
    st.fixed_dictionaries(
        {"type": st.sampled_from(ANTHROPIC_EVENTS)},
        optional={
            "index": st.one_of(st.integers(0, 3), json_values),
            "message": json_values,
            "content_block": json_values,
            "delta": json_values,
            "usage": json_values,
            "error": json_values,
        },
    ),
    max_size=8,
)
RESPONSES_EVENTS = [
    "response.created",
    "response.output_text.delta",
    "response.output_item.added",
    "response.function_call_arguments.delta",
    "response.completed",
    "response.incomplete",
    "response.failed",
    "error",
]
responses_events = st.lists(
    st.fixed_dictionaries(
        {"type": st.sampled_from(RESPONSES_EVENTS)},
        optional={
            "delta": json_values,
            "item": json_values,
            "output_index": json_values,
            "response": json_values,
            "item_id": json_values,
        },
    ),
    max_size=8,
)


@given(events=anthropic_events, usage=st.booleans())
def test_anthropic_stream_translation_fails_cleanly(events: list[Any], usage: bool) -> None:
    t = anthropic_format.StreamTranslator(include_usage=usage)
    try:
        for e in events:
            for chunk in t.feed(e):
                json.dumps(chunk)  # what goes to the client must be serialisable
    except ProviderError:
        pass


@given(events=responses_events, usage=st.booleans())
def test_responses_stream_translation_fails_cleanly(events: list[Any], usage: bool) -> None:
    t = openai_responses.StreamTranslator("openai", usage)
    try:
        for e in events:
            for chunk in t.feed(e):
                json.dumps(chunk)
    except ProviderError:
        pass


@given(msg=json_values)
def test_anthropic_responses_translate_or_fail_cleanly(msg: Any) -> None:
    if not isinstance(msg, dict):
        return
    try:
        json.dumps(anthropic_format.from_anthropic(msg))
    except ProviderError:
        pass


@given(response=json_values)
def test_responses_api_answers_translate_or_fail_cleanly(response: Any) -> None:
    if not isinstance(response, dict):
        return
    try:
        json.dumps(openai_responses.from_responses("openai", response))
    except ProviderError:
        pass


@given(msgs=messages, extra=extras)
def test_requests_translate_or_are_refused(msgs: Any, extra: Any) -> None:
    """Outbound: any request the API accepts either translates for Anthropic and the
    Responses API, or is refused as unsupported (→ 400 / next target), never a crash."""
    from pydantic import ValidationError

    from app.extensions import strip_request
    from app.schemas import ChatCompletionRequest

    try:
        validated = ChatCompletionRequest.model_validate(body("m", msgs, extra))
    except ValidationError:
        return  # the API answers 400 before any translator runs
    request = validated.upstream_body("m")
    for translate in (
        lambda: anthropic_format.to_anthropic(request, "m", {}, 1024),
        lambda: openai_responses.to_responses(dict(strip_request(request)), "m"),
    ):
        try:
            json.dumps(translate())
        except UnsupportedRequest:
            pass


# --- found by fuzzing (kept as plain regression tests) -----------------------------------------


def test_a_tool_message_without_its_call_id_is_a_400(api: TestClient) -> None:
    r = api.post(
        "/v1/chat/completions",
        json={"model": "claude-old", "messages": [{"role": "tool", "content": "42"}]},
    )
    assert r.status_code == 400 and "tool_call_id" in r.json()["error"]["message"]


def test_a_malformed_provider_event_is_a_provider_error() -> None:
    t = openai_responses.StreamTranslator("openai", True)
    with pytest.raises(ProviderError, match="invalid response"):
        t.feed({"type": "response.failed", "response": [None]})
    with pytest.raises(ProviderError, match="invalid response"):
        anthropic_format.from_anthropic({})
