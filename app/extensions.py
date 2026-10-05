"""Anthropic-only features carried through the internal (OpenAI) format, ADR 0013.

The internal format is OpenAI chat completions. Two Anthropic features matter too much to
drop when an Anthropic client (`/v1/messages`, e.g. Claude Code) talks to an Anthropic
target: prompt caching, which can cut an agent's input bill by most of it, and extended
thinking. They travel as extension fields that only the Anthropic adapter reads:

    request["thinking"]                        the Messages API `thinking` parameter
    part["cache_control"]                      on a content part (text, image)
    message["cache_control"]                   on a message whose content is a string, or
                                               a tool message (→ its tool_result block)
    tool["cache_control"]                      on a tool definition
    tool_call["cache_control"]                 on an assistant tool call (→ tool_use block)
    assistant_message["thinking_blocks"]       [{type: thinking, thinking, signature} |
                                                {type: redacted_thinking, data}]
    response message["thinking_blocks"]        the same, from the model's answer
    stream delta["thinking"]                   {index, start: {...}} | {index, thinking}
                                               | {index, signature}
    usage.prompt_tokens_details.cache_creation_tokens   prompt tokens written to cache

Everyone else must never see them: `strip_request` runs before any non-Anthropic
provider, and `strip_response` / `strip_chunk` before any OpenAI-format client.
"""

from __future__ import annotations

from typing import Any

REQUEST_FIELDS = ("thinking",)
MESSAGE_FIELDS = ("cache_control", "thinking_blocks")


def _strip_part(part: Any) -> Any:
    if isinstance(part, dict) and "cache_control" in part:
        return {k: v for k, v in part.items() if k != "cache_control"}
    return part


def strip_request(request: dict[str, Any]) -> dict[str, Any]:
    """A copy without extension fields (the caller's request is left alone)."""
    if not any(f in request for f in REQUEST_FIELDS) and not _has_extensions(request):
        return request
    out = {k: v for k, v in request.items() if k not in REQUEST_FIELDS}
    messages = []
    for msg in out.get("messages") or []:
        if not isinstance(msg, dict):
            messages.append(msg)
            continue
        m = {k: v for k, v in msg.items() if k not in MESSAGE_FIELDS}
        if isinstance(m.get("content"), list):
            m["content"] = [_strip_part(p) for p in m["content"]]
        if isinstance(m.get("tool_calls"), list):
            m["tool_calls"] = [_strip_part(c) for c in m["tool_calls"]]
        messages.append(m)
    if "messages" in out:
        out["messages"] = messages
    if isinstance(out.get("tools"), list):
        out["tools"] = [_strip_part(t) for t in out["tools"]]
    return out


def _has_extensions(request: dict[str, Any]) -> bool:
    for msg in request.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if any(f in msg for f in MESSAGE_FIELDS):
            return True
        for seq in (msg.get("content"), msg.get("tool_calls")):
            if isinstance(seq, list) and any(
                isinstance(p, dict) and "cache_control" in p for p in seq
            ):
                return True
    tools = request.get("tools")
    return isinstance(tools, list) and any(
        isinstance(t, dict) and "cache_control" in t for t in tools
    )


def strip_response(result: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat.completion without extension fields."""
    choices = result.get("choices")
    if not isinstance(choices, list) or not any(
        isinstance(c, dict) and "thinking_blocks" in (c.get("message") or {}) for c in choices
    ):
        return result
    out = dict(result)
    out["choices"] = [
        {
            **c,
            "message": {
                k: v for k, v in (c.get("message") or {}).items() if k != "thinking_blocks"
            },
        }
        for c in choices
    ]
    return out


def strip_chunk(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """An OpenAI chunk without extension fields; None if nothing is left to send."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not any(
        isinstance(c, dict) and "thinking" in (c.get("delta") or {}) for c in choices
    ):
        return chunk
    kept = []
    for c in choices:
        delta = {k: v for k, v in (c.get("delta") or {}).items() if k != "thinking"}
        if delta or c.get("finish_reason") is not None:
            kept.append({**c, "delta": delta})
    if not kept and not chunk.get("usage"):
        return None  # a thinking-only chunk: OpenAI clients get nothing
    return {**chunk, "choices": kept}
