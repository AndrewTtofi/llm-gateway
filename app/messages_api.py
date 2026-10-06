"""Inbound Anthropic Messages API (`POST /v1/messages`), ADR 0010.

Clients that speak Anthropic's format (the Anthropic SDKs, Claude Code) are translated
at the edge into the internal OpenAI chat-completions format, go through the same
pipeline (auth, limits, routing, fallback, metering), and the answer is translated back.
So a Messages request can be served by any provider, including a non-Anthropic fallback.

This is the reverse of `providers/anthropic_format.py` (OpenAI → Anthropic, outbound).
Things with no OpenAI equivalent are dropped: `cache_control` (so no prompt caching yet),
extended `thinking`, `top_k`, citations. Blocks that can't be expressed (documents,
server tools) are a 400 rather than being silently lost.

Pure functions and a stream encoder, no I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

from fastapi.responses import JSONResponse

# OpenAI finish_reason → Anthropic stop_reason.
STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

# Anthropic error type for each HTTP status (what the Anthropic SDKs map to exceptions).
ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
    504: "timeout_error",
    529: "overloaded_error",
}

EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


class InboundError(ValueError):
    """The Messages request can't be expressed internally → 400 for the client.

    The message may quote client values (a block type, a role), so it goes in the 400
    body only. Logs get `field`, a fixed name chosen here.
    """

    def __init__(self, message: str, field: str = "request") -> None:
        super().__init__(message)
        self.field = field


# --- request ---------------------------------------------------------------


def to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages request → OpenAI chat-completions request (internal format)."""
    try:
        return _to_openai(body)
    except InboundError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InboundError(f"malformed request: {type(exc).__name__}: {exc}", "malformed") from exc


def _to_openai(body: dict[str, Any]) -> dict[str, Any]:
    for field in ("model", "max_tokens", "messages"):
        if field not in body:
            raise InboundError(f"{field}: Field required", field)
    messages: list[dict[str, Any]] = []
    if system := _system_content(body.get("system")):
        messages.append({"role": "system", "content": system})
    for msg in body["messages"]:
        messages.extend(_message(msg))

    out: dict[str, Any] = {
        "model": body["model"],
        "max_tokens": body["max_tokens"],
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for name in ("temperature", "top_p"):
        if body.get(name) is not None:
            out[name] = body[name]
    if stop := body.get("stop_sequences"):
        out["stop"] = list(stop)
    if tools := body.get("tools"):
        out["tools"] = [_tool(t) for t in tools]
    if (choice := body.get("tool_choice")) is not None:
        out.update(_tool_choice(choice))
    if user := (body.get("metadata") or {}).get("user_id"):
        # Opaque and fixed-length for every provider (clients can send long ids).
        out["user"] = hashlib.sha256(str(user).encode()).hexdigest()
    effort = (body.get("output_config") or {}).get("effort")
    if effort in EFFORTS:
        out["reasoning_effort"] = effort
    if isinstance(thinking := body.get("thinking"), dict):
        out["thinking"] = thinking  # extension field: Anthropic targets only (ADR 0013)
    return out


def _system_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b["text"] for b in system if b.get("type") == "text")


def _system_content(system: Any) -> str | list[dict[str, Any]]:
    """The system prompt; as text parts when any block is marked for caching, so the
    cache breakpoint survives to Anthropic targets (ADR 0013)."""
    if isinstance(system, list) and any(
        isinstance(b, dict) and b.get("cache_control") for b in system
    ):
        return [
            _cached({"type": "text", "text": str(b.get("text", ""))}, b)
            for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    return _system_text(system)


def _cached(part: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    if cc := block.get("cache_control"):
        part["cache_control"] = cc
    return part


def _message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    role, content = msg["role"], msg["content"]
    if role == "system":
        # Mid-conversation system messages (sent by e.g. Claude Code). OpenAI has the same
        # role; the outbound Claude adapter folds it into the top-level system prompt.
        return [{"role": "system", "content": _system_content(content)}]
    if role not in ("user", "assistant"):
        raise InboundError(f"messages: role {role!r} is not supported", "messages.role")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if role == "assistant":
        return [_assistant(content)]

    # A user turn may carry tool results. In OpenAI those are separate `tool` messages,
    # which must come straight after the assistant's tool calls, so they go first and
    # any other content (text, images, images returned by tools) follows as a user turn.
    tool_msgs: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "tool_result":
            text, images = _tool_result(block.get("content"))
            if block.get("is_error"):
                text = f"[tool error] {text}"
            tool_msgs.append(
                _cached(
                    {"role": "tool", "tool_call_id": block["tool_use_id"], "content": text}, block
                )
            )
            parts.extend(images)
        else:
            parts.append(_user_part(block))
    out = tool_msgs
    if parts:
        out.append({"role": "user", "content": parts})
    return out


def _user_part(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("type")
    if kind == "text":
        return _cached({"type": "text", "text": block["text"]}, block)
    if kind == "image":
        return _cached(
            {"type": "image_url", "image_url": {"url": _image_url(block["source"])}}, block
        )
    raise InboundError(f"content block type {kind!r} is not supported", "content.type")


def _image_url(source: dict[str, Any]) -> str:
    if source.get("type") == "base64":
        return f"data:{source['media_type']};base64,{source['data']}"
    if source.get("type") == "url":
        return str(source["url"])
    raise InboundError(
        f"image source type {source.get('type')!r} is not supported", "image.source.type"
    )


def _tool_result(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for block in content:
        part = _user_part(block)
        if part["type"] == "text":
            texts.append(part["text"])
        else:
            images.append(part)  # OpenAI tool messages are text-only
    return "\n".join(texts), images


def _assistant(content: list[dict[str, Any]]) -> dict[str, Any]:
    texts: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    thinking: list[dict[str, Any]] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            texts.append(_cached({"type": "text", "text": block["text"]}, block))
        elif kind == "tool_use":
            calls.append(
                _cached(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    },
                    block,
                )
            )
        elif kind == "thinking":
            # Kept for Anthropic targets, which need earlier thinking (with its
            # signature) to continue a tool-using turn. Others never see it.
            thinking.append(
                {
                    "type": "thinking",
                    "thinking": block.get("thinking", ""),
                    "signature": block.get("signature", ""),
                }
            )
        elif kind == "redacted_thinking":
            thinking.append({"type": "redacted_thinking", "data": block.get("data", "")})
        else:
            raise InboundError(
                f"assistant content block type {kind!r} is not supported", "assistant.content.type"
            )
    # OpenAI rejects an assistant turn with neither content nor tool calls (e.g. one that
    # held only thinking blocks), so that keeps an empty string.
    text: str | list[dict[str, Any]] | None
    if any("cache_control" in t for t in texts):
        text = texts  # as parts, so the cache breakpoint survives
    else:
        text = "".join(t["text"] for t in texts) or (None if calls else "")
    out: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        out["tool_calls"] = calls
    if thinking:
        out["thinking_blocks"] = thinking
    return out


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") not in (None, "custom"):
        # Server tools (web search, code execution, …) run inside Anthropic's API and
        # can't be routed to other providers.
        raise InboundError(f"tool type {tool['type']!r} is not supported", "tools.type")
    fn: dict[str, Any] = {
        "name": tool["name"],
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }
    if desc := tool.get("description"):
        fn["description"] = desc
    if tool.get("strict"):
        fn["strict"] = True
    return _cached({"type": "function", "function": fn}, tool)


def _tool_choice(choice: dict[str, Any]) -> dict[str, Any]:
    kind = choice.get("type")
    out: dict[str, Any]
    if kind == "auto":
        out = {"tool_choice": "auto"}
    elif kind == "any":
        out = {"tool_choice": "required"}
    elif kind == "tool":
        out = {"tool_choice": {"type": "function", "function": {"name": choice["name"]}}}
    elif kind == "none":
        out = {"tool_choice": "none"}
    else:
        raise InboundError(f"tool_choice type {kind!r} is not supported", "tool_choice.type")
    if choice.get("disable_parallel_tool_use"):
        out["parallel_tool_calls"] = False
    return out


# --- response --------------------------------------------------------------


def usage_from_openai(usage: dict[str, Any] | None) -> dict[str, int]:
    """OpenAI's prompt_tokens includes cached tokens; Anthropic's input_tokens doesn't."""
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    written = int(details.get("cache_creation_tokens") or 0)  # extension (ADR 0013)
    prompt = int(usage.get("prompt_tokens") or 0)
    return {
        "input_tokens": max(0, prompt - cached - written),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": written,
    }


def _tool_input(arguments: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except ValueError:
        parsed = None
    # Anthropic's `input` is always an object; keep malformed arguments visible.
    return parsed if isinstance(parsed, dict) else {"_raw_arguments": arguments}


def _message_id(oai_id: str | None) -> str:
    return oai_id if oai_id and oai_id.startswith("msg_") else f"msg_{uuid.uuid4().hex[:24]}"


def from_openai(result: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat.completion → Anthropic message."""
    choice = (result.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    # Thinking first, as Anthropic returns it (and as it must be sent back next turn).
    content: list[dict[str, Any]] = [
        b for b in msg.get("thinking_blocks") or [] if isinstance(b, dict)
    ]
    if text := msg.get("content"):
        content.append({"type": "text", "text": text})
    if refusal := msg.get("refusal"):
        content.append({"type": "text", "text": refusal})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": _tool_input(fn.get("arguments")),
            }
        )
    return {
        "id": _message_id(result.get("id")),
        "type": "message",
        "role": "assistant",
        "model": result.get("model", ""),
        "content": content,
        # OpenAI doesn't say which stop sequence matched, so it's always null.
        "stop_reason": "refusal"
        if msg.get("refusal")
        else STOP_REASON.get(choice.get("finish_reason") or "", "end_turn"),
        "stop_sequence": None,
        "usage": usage_from_openai(result.get("usage")),
    }


def error_body(status: int, message: str, type_: str | None = None) -> dict[str, Any]:
    if type_ is None:
        type_ = ERROR_TYPE.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return {"type": "error", "error": {"type": type_, "message": message}}


def error_response(status: int, message: str, headers: Any = None) -> JSONResponse:
    return JSONResponse(error_body(status, message), status_code=status, headers=headers)


def convert_response(resp: JSONResponse) -> JSONResponse:
    """A finished OpenAI-format JSON response (answer or error) → Anthropic format,
    keeping status and headers (rate limits, x-gateway-*, retry-after)."""
    data = json.loads(bytes(resp.body))
    if resp.status_code < 400:
        content = from_openai(data)
    else:
        err = data.get("error") or {}
        # Budget exhausted: retrying won't help, unlike a rate limit (same 429 status).
        type_ = "billing_error" if err.get("code") == "insufficient_quota" else None
        content = error_body(resp.status_code, str(err.get("message") or "error"), type_)
    headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")
    }
    return JSONResponse(content, status_code=resp.status_code, headers=headers)


# --- streaming -------------------------------------------------------------


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"


class MessagesStream:
    """OpenAI chat.completion.chunk dicts → Anthropic stream events (a `StreamFormat`).

    Anthropic streams numbered content blocks, each opened, filled with deltas and
    closed: message_start, then content_block_start / content_block_delta… /
    content_block_stop per text or tool_use block, then message_delta (stop reason and
    usage) and message_stop. OpenAI streams flat deltas; this opens a block whenever the
    output changes kind (text ↔ tool calls) or a new tool call starts.

    Tool-call blocks stay open until the tool calls end (finish reason, or text again),
    then all close. Clients act on content_block_stop (an agent runs the tool), so a
    block must not close while its arguments can still arrive — and some providers
    interleave the arguments of parallel calls.

    Usage arrives in OpenAI's last chunk, after the finish reason, so message_delta
    waits for the end of the stream. `input_tokens` / `chars_per_token` give estimates
    for message_start (the real count isn't known yet) and for providers that send no
    usage at all.
    """

    def __init__(self, input_tokens: int = 0, chars_per_token: float = 4.0) -> None:
        self.input_tokens = input_tokens
        self.chars_per_token = chars_per_token
        self.started = False
        self.index = -1  # last block opened
        self.text_open = False
        self.thinking_open: dict[int, int] = {}  # source thinking block → our block index
        self.open_tools: list[int] = []  # block indices of open tool_use blocks
        self.tools: dict[int, tuple[str | None, int]] = {}  # OpenAI index → (id, block)
        self.stop_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.chars = 0

    def _start(self, chunk: dict[str, Any]) -> list[str]:
        self.started = True
        message = {
            "id": _message_id(chunk.get("id")),
            "type": "message",
            "role": "assistant",
            "model": chunk.get("model", ""),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            # An estimate: the provider's count arrives with message_delta.
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
        }
        return [_event("message_start", {"message": message})]

    def _close_text(self) -> list[str]:
        if not self.text_open:
            return []
        self.text_open = False
        return [_event("content_block_stop", {"index": self.index})]

    def _close_tools(self) -> list[str]:
        out = [_event("content_block_stop", {"index": i}) for i in self.open_tools]
        self.open_tools.clear()
        return out

    def _close_thinking(self) -> list[str]:
        out = [_event("content_block_stop", {"index": i}) for i in self.thinking_open.values()]
        self.thinking_open.clear()
        return out

    def _close_all(self) -> list[str]:
        return self._close_thinking() + self._close_text() + self._close_tools()

    def _thinking(self, t: dict[str, Any]) -> list[str]:
        """Extension delta from an Anthropic target (ADR 0013): thinking blocks, which
        must reach the client intact (text and signature) to be sent back next turn."""
        out: list[str] = []
        src = int(t.get("index") or 0)
        if isinstance(start := t.get("start"), dict):
            out += self._close_all()
            self.index += 1
            block = (
                {"type": "redacted_thinking", "data": start.get("data", "")}
                if start.get("type") == "redacted_thinking"
                else {"type": "thinking", "thinking": "", "signature": ""}
            )
            out.append(_event("content_block_start", {"index": self.index, "content_block": block}))
            self.thinking_open[src] = self.index
            return out
        idx = self.thinking_open.get(src)
        if idx is None:
            return out
        if text := t.get("thinking"):
            delta = {"type": "thinking_delta", "thinking": text}
            out.append(_event("content_block_delta", {"index": idx, "delta": delta}))
        if sig := t.get("signature"):
            delta = {"type": "signature_delta", "signature": sig}
            out.append(_event("content_block_delta", {"index": idx, "delta": delta}))
        return out

    def _open(self, block: dict[str, Any]) -> list[str]:
        self.index += 1
        return [_event("content_block_start", {"index": self.index, "content_block": block})]

    def encode(self, chunk: dict[str, Any]) -> list[str]:
        out = [] if self.started else self._start(chunk)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(thinking := delta.get("thinking"), dict):
                out += self._thinking(thinking)
            if text := delta.get("content"):
                self.chars += len(text)
                if not self.text_open:
                    out += self._close_thinking() + self._close_tools()
                    out += self._open({"type": "text", "text": ""})
                    self.text_open = True
                out.append(
                    _event(
                        "content_block_delta",
                        {"index": self.index, "delta": {"type": "text_delta", "text": text}},
                    )
                )
            for call in delta.get("tool_calls") or []:
                out += self._tool_delta(call)
            if reason := choice.get("finish_reason"):
                self.stop_reason = STOP_REASON.get(reason, "end_turn")
                out += self._close_all()
        return out

    def _tool_delta(self, call: dict[str, Any]) -> list[str]:
        out: list[str] = []
        i = call.get("index")
        i = int(i) if isinstance(i, int) else len(self.tools)  # no index: a new call
        fn = call.get("function") or {}
        known = self.tools.get(i)
        # A different id at a known index is a new call too (providers that send every
        # call as index 0); arguments without an id belong to the latest call there.
        if known is None or (call.get("id") and call["id"] != known[0]):
            out += self._close_thinking() + self._close_text()
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name") or "",
                "input": {},
            }
            out += self._open(block)
            self.tools[i] = (call.get("id"), self.index)
            self.open_tools.append(self.index)
        if args := fn.get("arguments"):
            self.chars += len(args)
            delta = {"type": "input_json_delta", "partial_json": args}
            out.append(_event("content_block_delta", {"index": self.tools[i][1], "delta": delta}))
        return out

    def _final_usage(self) -> dict[str, int]:
        if self.usage is not None:
            return usage_from_openai(self.usage)
        # The provider sent no usage: report the same estimates the meter bills.
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": math.ceil(self.chars / self.chars_per_token),
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def end(self) -> list[str]:
        out = [] if self.started else self._start({})
        out += self._close_all()
        delta = {"stop_reason": self.stop_reason or "end_turn", "stop_sequence": None}
        out.append(_event("message_delta", {"delta": delta, "usage": self._final_usage()}))
        out.append(_event("message_stop", {}))
        return out

    def error(self, message: str) -> str:
        return _event("error", {"error": {"type": "api_error", "message": message}})
