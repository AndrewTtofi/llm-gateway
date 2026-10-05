"""OpenAI ⇄ Anthropic translation, one rule per test."""

import json
from typing import Any

import pytest

from app.providers.anthropic_format import (
    StreamTranslator,
    TranslationError,
    caps_for,
    from_anthropic,
    to_anthropic,
    usage_to_openai,
)

OLD = {"sampling": True, "forced_tool_choice": True, "effort": False}
NEW = {"sampling": False, "forced_tool_choice": False, "effort": True}
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def conv(*msgs: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"model": "alias", "messages": list(msgs), **extra}


def tx(req: dict[str, Any], caps: dict[str, Any] = OLD) -> dict[str, Any]:
    return to_anthropic(req, "m", caps, default_max_tokens=1000)


# --- request ---------------------------------------------------------------


def test_system_and_developer_messages_become_top_level_system() -> None:
    out = tx(
        conv(
            {"role": "system", "content": "Be brief."},
            {"role": "developer", "content": "Use metric."},
            {"role": "user", "content": "hi"},
        )
    )
    assert out["system"] == "Be brief.\n\nUse metric."
    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_max_tokens_required_so_defaulted_and_newer_param_wins() -> None:
    assert tx(conv({"role": "user", "content": "x"}))["max_tokens"] == 1000
    assert tx(conv({"role": "user", "content": "x"}, max_tokens=50))["max_tokens"] == 50
    out = tx(conv({"role": "user", "content": "x"}, max_tokens=50, max_completion_tokens=70))
    assert out["max_tokens"] == 70


def test_stop_string_becomes_list() -> None:
    assert tx(conv({"role": "user", "content": "x"}, stop="END"))["stop_sequences"] == ["END"]


def test_sampling_params_clamped_or_dropped_by_capability() -> None:
    req = conv({"role": "user", "content": "x"}, temperature=1.7, top_p=0.9)
    # 0–1 range; Claude 4+ rejects temperature and top_p together → temperature wins
    assert tx(req, OLD)["extra_body"] == {"temperature": 1.0}
    only_p = conv({"role": "user", "content": "x"}, top_p=0.9)
    assert tx(only_p, OLD)["extra_body"] == {"top_p": 0.9}
    assert "extra_body" not in tx(req, NEW)  # newer models 400 on these


def test_reasoning_effort_only_where_supported() -> None:
    req = conv({"role": "user", "content": "x"}, reasoning_effort="minimal")
    assert tx(req, NEW)["output_config"] == {"effort": "low"}
    assert "output_config" not in tx(req, OLD)


def test_json_schema_response_format() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    req = conv(
        {"role": "user", "content": "x"},
        response_format={"type": "json_schema", "json_schema": {"name": "s", "schema": schema}},
    )
    sent = tx(req)["output_config"]["format"]["schema"]
    assert sent["additionalProperties"] is False  # required by Anthropic, added for the client


def test_json_schema_constraints_are_rewritten() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
    req = conv(
        {"role": "user", "content": "x"},
        response_format={"type": "json_schema", "json_schema": {"name": "s", "schema": schema}},
    )
    assert "minimum" not in tx(req)["output_config"]["format"]["schema"]["properties"]["n"]


def test_json_schema_without_schema_falls_back_to_instruction() -> None:
    req = conv(
        {"role": "user", "content": "x"},
        response_format={"type": "json_schema", "json_schema": {"name": "s"}},
    )
    out = tx(req)
    assert "output_config" not in out and "JSON" in out["system"]


def test_json_object_response_format_becomes_instruction() -> None:
    out = tx(conv({"role": "user", "content": "x"}, response_format={"type": "json_object"}))
    assert "JSON" in out["system"]


def test_images_data_uri_and_url() -> None:
    out = tx(
        conv(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.jpg"}},
                ],
            }
        )
    )
    blocks = out["messages"][0]["content"]
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert blocks[2]["source"] == {"type": "url", "url": "https://x.test/a.jpg"}


def test_unsupported_content_part_is_400() -> None:
    with pytest.raises(TranslationError):
        tx(conv({"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}))


def test_n_greater_than_one_is_rejected() -> None:
    with pytest.raises(TranslationError):
        tx(conv({"role": "user", "content": "x"}, n=2))


def test_tools_translate_and_stream_eagerly() -> None:
    out = tx(conv({"role": "user", "content": "x"}, tools=[TOOL], stream=True))
    assert out["tools"] == [
        {
            "name": "get_weather",
            "description": "Weather for a city",
            "input_schema": TOOL["function"]["parameters"],
            "eager_input_streaming": True,
        }
    ]
    assert out["tool_choice"] == {"type": "auto"}


def test_forced_tool_choice_where_supported() -> None:
    named = {"type": "function", "function": {"name": "get_weather"}}
    assert tx(conv({"role": "user", "content": "x"}, tools=[TOOL], tool_choice=named), OLD)[
        "tool_choice"
    ] == {"type": "tool", "name": "get_weather"}
    assert tx(conv({"role": "user", "content": "x"}, tools=[TOOL], tool_choice="required"), OLD)[
        "tool_choice"
    ] == {"type": "any"}


def test_forced_tool_choice_downgraded_on_models_that_reject_it() -> None:
    named = {"type": "function", "function": {"name": "get_weather"}}
    out = tx(
        conv(
            {"role": "user", "content": "x"},
            tools=[TOOL],
            tool_choice=named,
            parallel_tool_calls=False,
        ),
        NEW,
    )
    assert out["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "`get_weather`" in out["system"]


def test_tool_call_round_trip_and_parallel_results_merge() -> None:
    out = tx(
        conv(
            {"role": "user", "content": "weather in Paris and Rome?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "18C"},
            {"role": "tool", "tool_call_id": "c2", "content": "24C"},
        )
    )
    user, assistant, results = out["messages"]
    assert [b["input"] for b in assistant["content"]] == [{"city": "Paris"}, {"city": "Rome"}]
    # Both results must be in ONE user message, or the model learns to stop calling in parallel.
    assert results["role"] == "user"
    assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]


def test_invalid_tool_call_arguments_are_400() -> None:
    with pytest.raises(TranslationError):
        tx(
            conv(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{nope"},
                        }
                    ],
                }
            )
        )


def test_user_is_hashed_into_metadata() -> None:
    uid = tx(conv({"role": "user", "content": "x"}, user="bob@example.com"))["metadata"]["user_id"]
    assert "bob" not in uid and len(uid) == 64  # opaque, no PII sent upstream


@pytest.mark.parametrize(
    "msgs",
    [
        [{"role": "system", "content": "Only a system prompt."}],
        [{"role": "assistant", "content": "Hi! How can I help?"}, {"role": "user", "content": "x"}],
    ],
)
def test_first_message_is_always_user(msgs: list[dict[str, Any]]) -> None:
    out = tx(conv(*msgs))
    assert out["messages"][0]["role"] == "user"


@pytest.mark.parametrize(
    "bad",
    [
        {"tools": [{"type": "function"}]},  # no "function" object
        {"max_tokens": "lots"},
    ],
)
def test_malformed_params_are_400_not_500(bad: dict[str, Any]) -> None:
    with pytest.raises(TranslationError):
        tx(conv({"role": "user", "content": "x"}, **bad))


def test_tool_message_without_call_id_is_400() -> None:
    with pytest.raises(TranslationError):
        tx(conv({"role": "user", "content": "x"}, {"role": "tool", "content": "r"}))


def test_jpg_media_type_normalised_and_non_images_rejected() -> None:
    def img(url: str) -> dict[str, Any]:
        return conv({"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]})

    out = tx(img("data:image/jpg;base64,AAAA"))
    assert out["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
    with pytest.raises(TranslationError):
        tx(img("data:application/pdf;base64,AAAA"))


def test_strict_tool_schema_rewritten() -> None:
    strict = {"type": "function", "function": {**TOOL["function"], "strict": True}}
    out = tx(conv({"role": "user", "content": "x"}, tools=[strict]))
    assert out["tools"][0]["strict"] is True
    assert out["tools"][0]["input_schema"]["additionalProperties"] is False


def test_caps_overlay_defaults() -> None:
    cfg = {"defaults": {"sampling": True, "effort": False}, "models": {"m": {"effort": True}}}
    assert caps_for(cfg, "m") == {"sampling": True, "effort": True}
    assert caps_for(cfg, "other") == {"sampling": True, "effort": False}


# --- response --------------------------------------------------------------

MSG = {
    "id": "msg_1",
    "model": "claude-x",
    "role": "assistant",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Oslo"}},
    ],
    "stop_reason": "tool_use",
    "usage": {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 90,
        "cache_creation_input_tokens": 0,
    },
}


def test_response_text_tools_and_dropped_thinking() -> None:
    out = from_anthropic(MSG)
    choice = out["choices"][0]
    assert choice["message"]["content"] == "Checking."
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "toolu_1" and json.loads(call["function"]["arguments"]) == {"city": "Oslo"}
    assert choice["finish_reason"] == "tool_calls"


@pytest.mark.parametrize(
    ("stop", "finish"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("stop_sequence", "stop"),
        ("refusal", "content_filter"),
        ("model_context_window_exceeded", "length"),
    ],
)
def test_stop_reasons(stop: str, finish: str) -> None:
    assert from_anthropic({**MSG, "stop_reason": stop})["choices"][0]["finish_reason"] == finish


def test_usage_counts_cached_tokens_as_prompt_tokens() -> None:
    u = usage_to_openai(MSG["usage"])
    assert u["prompt_tokens"] == 100 and u["completion_tokens"] == 5 and u["total_tokens"] == 105
    assert u["prompt_tokens_details"] == {"cached_tokens": 90}


# --- streaming -------------------------------------------------------------

EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "model": "claude-x",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": ""},
    },
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
    {
        "type": "content_block_start",
        "index": 2,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": '{"a":'},
    },
    {
        "type": "content_block_delta",
        "index": 2,
        "delta": {"type": "input_json_delta", "partial_json": " 1}"},
    },
    {"type": "ping"},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
]


def run(include_usage: bool) -> list[dict[str, Any]]:
    t = StreamTranslator(include_usage=include_usage)
    return [c for e in EVENTS for c in t.feed(e)]


def test_stream_text_tool_calls_and_finish() -> None:
    chunks = run(include_usage=False)
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(d.get("content") or "" for d in deltas) == "Hello"
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert calls[0]["index"] == 0  # renumbered: it was content block 2
    assert calls[0]["id"] == "toolu_1" and calls[0]["function"]["name"] == "f"
    assert "".join(tc["function"]["arguments"] for tc in calls) == '{"a": 1}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all(c["id"] == "msg_1" and c["model"] == "claude-x" for c in chunks)


def test_stream_usage_only_when_asked() -> None:
    assert not any("usage" in c for c in run(include_usage=False))
    final = run(include_usage=True)[-1]
    assert final["choices"] == []
    assert final["usage"]["prompt_tokens"] == 10 and final["usage"]["completion_tokens"] == 9


def feed_all(t: StreamTranslator, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for e in events for c in t.feed(e)]


def tool_calls(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        tc for c in chunks if c["choices"] for tc in c["choices"][0]["delta"].get("tool_calls", [])
    ]


def tool_block(index: int, call_id: str, *parts: str) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": "f", "input": {}},
        }
    ]
    evs += [
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": p},
        }
        for p in parts
    ]
    return [*evs, {"type": "content_block_stop", "index": index}]


START = {"type": "message_start", "message": {"id": "m", "model": "x", "usage": {}}}
END = [
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
]


def args_by_index(chunks: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for tc in tool_calls(chunks):
        out[tc["index"]] = out.get(tc["index"], "") + tc["function"].get("arguments", "")
    return out


def test_stream_no_argument_tool_call_sends_empty_object() -> None:
    chunks = feed_all(StreamTranslator(False), [START, *tool_block(0, "t1", ""), *END])
    assert args_by_index(chunks) == {0: "{}"}


def test_stream_multiple_tool_calls_renumbered() -> None:
    events = [START, *tool_block(1, "t1", '{"a": 1}'), *tool_block(3, "t2", '{"b": 2}'), *END]
    chunks = feed_all(StreamTranslator(False), events)
    assert args_by_index(chunks) == {0: '{"a": 1}', 1: '{"b": 2}'}
    assert [tc["id"] for tc in tool_calls(chunks) if "id" in tc] == ["t1", "t2"]


@pytest.mark.parametrize("buffer", [False, True])
def test_stream_completed_only_after_message_stop(buffer: bool) -> None:
    t = StreamTranslator(False, buffer_tools=buffer)
    feed_all(t, [START, *tool_block(0, "t1", "{}")])
    assert not t.completed
    feed_all(t, END)
    assert t.completed


def test_buffered_tools_drop_calls_from_a_declined_model() -> None:
    fallback = {
        "type": "content_block_start",
        "index": 2,
        "content_block": {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
    }
    events = [
        START,
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Let me "},
        },
        *tool_block(1, "declined", '{"half": '),  # declined mid-tool-call
        fallback,
        *tool_block(3, "kept", '{"ok": true}'),
        *END,
    ]
    chunks = feed_all(StreamTranslator(False, buffer_tools=True), events)
    calls = tool_calls(chunks)
    assert [c["id"] for c in calls] == ["kept"] and calls[0]["index"] == 0
    assert calls[0]["function"]["arguments"] == '{"ok": true}'
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text == "Let me "  # text before the boundary stays
    # tool calls arrive before the finish chunk
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_usage_iterations_kept_for_cost_tracking() -> None:
    iters = [
        {"type": "message", "input_tokens": 5},
        {"type": "fallback_message", "input_tokens": 6},
    ]
    assert (
        usage_to_openai({"input_tokens": 6, "output_tokens": 1, "iterations": iters})["iterations"]
        == iters
    )
