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

import json
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
    529: "overloaded_error",
}

EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


class InboundError(ValueError):
    """The Messages request can't be expressed internally → 400 for the client."""


# --- request ---------------------------------------------------------------


def to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages request → OpenAI chat-completions request (internal format)."""
    try:
        return _to_openai(body)
    except InboundError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InboundError(f"malformed request: {type(exc).__name__}: {exc}") from exc


def _to_openai(body: dict[str, Any]) -> dict[str, Any]:
    for field in ("model", "max_tokens", "messages"):
        if field not in body:
            raise InboundError(f"{field}: Field required")
    messages: list[dict[str, Any]] = []
    if system := _system_text(body.get("system")):
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
        out["user"] = str(user)
    effort = (body.get("output_config") or {}).get("effort")
    if effort in EFFORTS:
        out["reasoning_effort"] = effort
    return out


def _system_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b["text"] for b in system if b.get("type") == "text")


def _message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    role, content = msg["role"], msg["content"]
    if role == "system":
        # Mid-conversation system messages (sent by e.g. Claude Code). OpenAI has the same
        # role; the outbound Claude adapter folds it into the top-level system prompt.
        return [{"role": "system", "content": _system_text(content)}]
    if role not in ("user", "assistant"):
        raise InboundError(f"messages: role {role!r} is not supported")
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
                {"role": "tool", "tool_call_id": block["tool_use_id"], "content": text}
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
        return {"type": "text", "text": block["text"]}
    if kind == "image":
        return {"type": "image_url", "image_url": {"url": _image_url(block["source"])}}
    raise InboundError(f"content block type {kind!r} is not supported")


def _image_url(source: dict[str, Any]) -> str:
    if source.get("type") == "base64":
        return f"data:{source['media_type']};base64,{source['data']}"
    if source.get("type") == "url":
        return str(source["url"])
    raise InboundError(f"image source type {source.get('type')!r} is not supported")


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
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            texts.append(block["text"])
        elif kind == "tool_use":
            calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                }
            )
        elif kind in ("thinking", "redacted_thinking"):
            continue  # earlier reasoning: no OpenAI equivalent, and not needed to continue
        else:
            raise InboundError(f"assistant content block type {kind!r} is not supported")
    out: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if calls:
        out["tool_calls"] = calls
    return out


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") not in (None, "custom"):
        # Server tools (web search, code execution, …) run inside Anthropic's API and
        # can't be routed to other providers.
        raise InboundError(f"tool type {tool['type']!r} is not supported")
    fn: dict[str, Any] = {
        "name": tool["name"],
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }
    if desc := tool.get("description"):
        fn["description"] = desc
    if tool.get("strict"):
        fn["strict"] = True
    return {"type": "function", "function": fn}


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
        raise InboundError(f"tool_choice type {kind!r} is not supported")
    if choice.get("disable_parallel_tool_use"):
        out["parallel_tool_calls"] = False
    return out


# --- response --------------------------------------------------------------


def usage_from_openai(usage: dict[str, Any] | None) -> dict[str, int]:
    """OpenAI's prompt_tokens includes cached tokens; Anthropic's input_tokens doesn't."""
    usage = usage or {}
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
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
    content: list[dict[str, Any]] = []
    if text := msg.get("content"):
        content.append({"type": "text", "text": text})
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
        "stop_reason": STOP_REASON.get(choice.get("finish_reason") or "", "end_turn"),
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
        content = error_body(resp.status_code, str(err.get("message") or "error"))
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
    usage) and message_stop. OpenAI streams flat deltas; this opens a new block whenever
    the kind of output changes (text → tool call, or one tool call → the next).

    Usage arrives in OpenAI's last chunk, after the finish reason, so message_delta
    waits for the end of the stream.
    """

    def __init__(self) -> None:
        self.started = False
        self.index = -1  # current content block
        self.open: str | int | None = None  # "text", an OpenAI tool-call index, or None
        self.tool_blocks: dict[int, int] = {}  # OpenAI tool-call index → block index
        self.stop_reason: str | None = None
        self.usage: dict[str, Any] | None = None

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
            "usage": {"input_tokens": 0, "output_tokens": 0},  # real numbers in message_delta
        }
        return [_event("message_start", {"message": message})]

    def _close(self) -> list[str]:
        if self.open is None:
            return []
        self.open = None
        return [_event("content_block_stop", {"index": self.index})]

    def _open(self, block: dict[str, Any], kind: str | int) -> list[str]:
        out = self._close()
        self.index += 1
        self.open = kind
        out.append(_event("content_block_start", {"index": self.index, "content_block": block}))
        return out

    def encode(self, chunk: dict[str, Any]) -> list[str]:
        out = [] if self.started else self._start(chunk)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if text := delta.get("content"):
                if self.open != "text":
                    out += self._open({"type": "text", "text": ""}, "text")
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
        return out

    def _tool_delta(self, call: dict[str, Any]) -> list[str]:
        out: list[str] = []
        i = int(call.get("index") or 0)
        fn = call.get("function") or {}
        if i not in self.tool_blocks:
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name") or "",
                "input": {},
            }
            out += self._open(block, i)
            self.tool_blocks[i] = self.index
        if args := fn.get("arguments"):
            # Providers stream one tool call after another; arguments for an earlier
            # call are still sent to its own block index.
            delta = {"type": "input_json_delta", "partial_json": args}
            out.append(
                _event("content_block_delta", {"index": self.tool_blocks[i], "delta": delta})
            )
        return out

    def end(self) -> list[str]:
        out = [] if self.started else self._start({})
        out += self._close()
        delta = {"stop_reason": self.stop_reason or "end_turn", "stop_sequence": None}
        out.append(
            _event("message_delta", {"delta": delta, "usage": usage_from_openai(self.usage)})
        )
        out.append(_event("message_stop", {}))
        return out

    def error(self, message: str) -> str:
        return _event("error", {"error": {"type": "api_error", "message": message}})
