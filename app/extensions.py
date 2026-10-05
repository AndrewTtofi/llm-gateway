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
    usage.prompt_tokens_details.cache_creation_1h_tokens  … of which to the 1-hour cache

Everyone else must never see them: `strip_request` runs before any non-Anthropic
provider, and `strip_response` / `strip_chunk` before any OpenAI-format client.
"""

from __future__ import annotations

from typing import Any

REQUEST_FIELDS = ("thinking", "route")  # route: policy-routing hints (ADR 0017)
MESSAGE_FIELDS = ("cache_control", "thinking_blocks")


def _strip_part(part: Any) -> Any:
    if isinstance(part, dict) and "cache_control" in part:
        return {k: v for k, v in part.items() if k != "cache_control"}
    return part


USAGE_FIELDS = ("cache_creation_tokens", "cache_creation_1h_tokens")


def _joined_text(content: list[Any]) -> str | None:
    """Text-only parts as one string (None if any part isn't text)."""
    if all(isinstance(p, dict) and p.get("type") == "text" for p in content):
        return "".join(p.get("text", "") for p in content)
    return None


def without_thinking(request: dict[str, Any]) -> dict[str, Any]:
    """For OpenAI-format clients: they can't receive thinking blocks back (they're stripped
    from responses), so they mustn't turn thinking on either, or the next tool-use turn
    would lack the signed blocks Anthropic requires. cache_control is fine to keep."""
    if "thinking" not in request and not any(
        isinstance(m, dict) and "thinking_blocks" in m for m in request.get("messages") or []
    ):
        return request
    out = {k: v for k, v in request.items() if k != "thinking"}
    out["messages"] = [
        {k: v for k, v in m.items() if k != "thinking_blocks"} if isinstance(m, dict) else m
        for m in request.get("messages") or []
    ]
    return out


def strip_request(request: dict[str, Any]) -> dict[str, Any]:
    """A copy without extension fields (the caller's request is left alone). Text-only
    part lists on system, developer and assistant messages go back to plain strings: they
    were lists only to carry cache breakpoints, and not every OpenAI-compatible server
    accepts lists there."""
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
            if m.get("role") in ("system", "developer", "assistant"):
                if (text := _joined_text(m["content"])) is not None:
                    m["content"] = text
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


def _strip_usage(usage: Any) -> Any:
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    if not isinstance(details, dict) or not any(f in details for f in USAGE_FIELDS):
        return usage
    return {
        **usage,
        "prompt_tokens_details": {k: v for k, v in details.items() if k not in USAGE_FIELDS},
    }


def strip_response(result: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI chat.completion without extension fields (the meter has read them)."""
    if "usage" in result:
        result = {**result, "usage": _strip_usage(result["usage"])}
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
    if "usage" in chunk:
        chunk = {**chunk, "usage": _strip_usage(chunk["usage"])}
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
