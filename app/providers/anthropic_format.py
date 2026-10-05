"""Translation between OpenAI chat-completions and Anthropic's Messages API.

Pure functions, no I/O — the adapter in `anthropic.py` does the transport.
The main differences being bridged:

- System prompt is a top-level `system` field, not a message.
- Tool calls are content blocks (`tool_use`) inside the assistant message, and tool
  results are `tool_result` blocks inside a *user* message — not separate roles.
- `max_tokens` is required.
- Stop reasons, usage fields and stream events have different names and shapes.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from anthropic import transform_schema

from app.providers.base import UnsupportedRequest

# finish_reason for each Anthropic stop_reason.
FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",  # a safety classifier declined
}

IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
IMAGE_TYPE_ALIASES = {"image/jpg": "image/jpeg"}

# OpenAI reasoning_effort → Anthropic output_config.effort.
EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}


class TranslationError(UnsupportedRequest):
    """The request uses something we can't express for Anthropic → client gets a 400."""


def caps_for(cfg: dict[str, Any], model: str) -> dict[str, Any]:
    """Model capabilities from config: provider `defaults` overlaid with `models.<model>`."""
    return {**cfg.get("defaults", {}), **cfg.get("models", {}).get(model, {})}


# --- request ---------------------------------------------------------------


def to_anthropic(
    request: dict[str, Any], model: str, caps: dict[str, Any], default_max_tokens: int
) -> dict[str, Any]:
    """OpenAI chat request → kwargs for `messages.create`. Unknown OpenAI params are dropped.

    Malformed input (missing keys, wrong types) is a 400 for the client, not a 500.
    """
    try:
        return _to_anthropic(request, model, caps, default_max_tokens)
    except TranslationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise TranslationError(f"malformed request: {type(exc).__name__}: {exc}") from exc


def _to_anthropic(
    request: dict[str, Any], model: str, caps: dict[str, Any], default_max_tokens: int
) -> dict[str, Any]:
    if request.get("n", 1) != 1:
        raise TranslationError("n > 1 is not supported for this model")

    # System prompt as blocks, so cache breakpoints survive (ADR 0013); sent as one
    # string when nothing in it is marked for caching.
    system_parts: list[dict[str, Any]] = []
    messages = _messages(request["messages"], system_parts)

    out: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(
            request.get("max_completion_tokens") or request.get("max_tokens") or default_max_tokens
        ),
    }
    extra: dict[str, Any] = {}

    stop = request.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    if caps.get("sampling", True):
        # Anthropic's temperature range is 0–1 (OpenAI's is 0–2), so clamp. Claude 4+
        # rejects temperature and top_p together, so temperature wins.
        if (t := request.get("temperature")) is not None:
            extra["temperature"] = min(float(t), 1.0)
        elif (p := request.get("top_p")) is not None:
            extra["top_p"] = float(p)

    output_config: dict[str, Any] = {}
    if caps.get("effort") and (effort := EFFORT.get(str(request.get("reasoning_effort")))):
        output_config["effort"] = effort

    fmt = request.get("response_format") or {}
    schema = (
        (fmt.get("json_schema") or {}).get("schema") if fmt.get("type") == "json_schema" else None
    )
    if schema:
        # Anthropic needs additionalProperties:false everywhere and rejects min/max-style
        # constraints; the SDK rewrites OpenAI-style schemas to fit.
        output_config["format"] = {"type": "json_schema", "schema": transform_schema(schema)}
    elif fmt.get("type") in ("json_object", "json_schema"):
        system_parts.append(_text("Respond with a single valid JSON object and nothing else."))
    if output_config:
        out["output_config"] = output_config

    if tools := request.get("tools"):
        out["tools"] = [_tool(t, stream=bool(request.get("stream"))) for t in tools]
        if (choice := _tool_choice(request, caps, system_parts)) is not None:
            out["tool_choice"] = choice

    if user := request.get("user"):
        # Clients often put an email here; Anthropic wants an opaque id. Hash it.
        out["metadata"] = {"user_id": hashlib.sha256(str(user).encode()).hexdigest()}
    if isinstance(thinking := request.get("thinking"), dict):
        out["thinking"] = thinking  # extension field from /v1/messages clients (ADR 0013)
    if system_parts:
        if any("cache_control" in b for b in system_parts):
            out["system"] = system_parts
        else:
            out["system"] = "\n\n".join(b["text"] for b in system_parts)
    if extra:
        out["extra_body"] = extra
    return out


def _text(text: str, cache_control: Any = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache_control:
        block["cache_control"] = cache_control
    return block


def _messages(
    oai: list[dict[str, Any]], system_parts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        # Merge consecutive same-role turns. Parallel tool results in particular must
        # arrive as one user message with several tool_result blocks.
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in oai:
        role = msg["role"]
        if role in ("system", "developer"):
            # Collected into the top-level system prompt. A system message in the middle
            # of the conversation therefore moves to the front.
            content = msg.get("content")
            if isinstance(content, list):
                system_parts.extend(
                    _text(p.get("text", ""), p.get("cache_control"))
                    for p in content
                    if p.get("type") == "text" and p.get("text")
                )
            elif text := _text_of(content):
                system_parts.append(_text(text, msg.get("cache_control")))
        elif role == "user":
            append("user", _content_blocks(msg.get("content"), msg.get("cache_control")))
        elif role == "assistant":
            # Earlier thinking (with signatures) goes first, as the API returned it.
            blocks = [b for b in msg.get("thinking_blocks") or [] if isinstance(b, dict)]
            blocks += _content_blocks(msg.get("content"), msg.get("cache_control"))
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError as exc:
                    raise TranslationError(f"tool call {call.get('id')} has invalid JSON") from exc
                tool_use = {"type": "tool_use", "id": call["id"], "name": fn["name"], "input": args}
                if cc := call.get("cache_control"):
                    tool_use["cache_control"] = cc
                blocks.append(tool_use)
            append("assistant", blocks)
        elif role == "tool":
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": _text_of(msg.get("content")),
            }
            if cc := msg.get("cache_control"):
                result["cache_control"] = cc
            append("user", [result])
    # The Messages API needs a user turn first (and at least one message). OpenAI allows
    # system-only requests and conversations opening with a seeded assistant greeting.
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
    return out


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if p.get("type") == "text")


def _content_blocks(content: Any, cache_control: Any = None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [_text(content, cache_control)] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type")
        if kind in ("text", "refusal") and (text := part.get("text") or part.get("refusal")):
            blocks.append(_text(text, part.get("cache_control")))
        elif kind == "image_url":
            image = _image(part["image_url"]["url"])
            if cc := part.get("cache_control"):
                image["cache_control"] = cc
            blocks.append(image)
        else:
            raise TranslationError(f"content part type {kind!r} is not supported for this model")
    return blocks


def _image(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        # data:image/png;base64,iVBOR…
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0].lower()
        media_type = IMAGE_TYPE_ALIASES.get(media_type, media_type)
        if media_type not in IMAGE_TYPES:
            raise TranslationError(f"image type {media_type!r} is not supported")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _tool(tool: dict[str, Any], stream: bool) -> dict[str, Any]:
    if tool.get("type") != "function":
        raise TranslationError(f"tool type {tool.get('type')!r} is not supported")
    fn = tool["function"]
    out: dict[str, Any] = {
        "name": fn["name"],
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
    if desc := fn.get("description"):
        out["description"] = desc
    if fn.get("strict"):
        out["strict"] = True
        out["input_schema"] = transform_schema(out["input_schema"])
    if cc := tool.get("cache_control"):
        out["cache_control"] = cc
    if stream:
        # Stream tool arguments as they're generated (like OpenAI does) instead of in
        # one burst at the end. Clients must validate the final arguments either way.
        out["eager_input_streaming"] = True
    return out


def _tool_choice(
    request: dict[str, Any], caps: dict[str, Any], system_parts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    choice = request.get("tool_choice")
    no_parallel = request.get("parallel_tool_calls") is False
    forced: str | None = None  # tool name, or "" for "any tool"

    if choice is None or choice == "auto":
        out: dict[str, Any] = {"type": "auto"}
    elif choice == "none":
        return {"type": "none"}
    elif choice == "required":
        out, forced = {"type": "any"}, ""
    elif isinstance(choice, dict) and choice.get("type") == "function":
        name = choice["function"]["name"]
        out, forced = {"type": "tool", "name": name}, name
    else:
        raise TranslationError(f"tool_choice {choice!r} is not supported")

    if forced is not None and not caps.get("forced_tool_choice", True):
        # Some models reject forced tool use (400). Fall back to auto and ask in the
        # prompt — a strong nudge, not a guarantee.
        out = {"type": "auto"}
        system_parts.append(
            _text(
                f"You must call the `{forced}` tool in your response."
                if forced
                else "You must call one of the provided tools in your response."
            )
        )
    if no_parallel:
        out["disable_parallel_tool_use"] = True
    return out


# --- response --------------------------------------------------------------


def usage_to_openai(usage: dict[str, Any]) -> dict[str, Any]:
    """Anthropic's input_tokens excludes cached tokens; OpenAI's prompt_tokens includes them."""
    cached = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    prompt = (usage.get("input_tokens") or 0) + cached + written
    completion = usage.get("output_tokens") or 0
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        # cache_creation_tokens: an extension, billed at the cache-write price (ADR 0013)
        "prompt_tokens_details": {"cached_tokens": cached, "cache_creation_tokens": written},
    }
    if written_1h := int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0):
        out["prompt_tokens_details"]["cache_creation_1h_tokens"] = written_1h  # dearer writes
    if iterations := usage.get("iterations"):
        # With a refusal fallback, top-level usage covers only the attempt that produced
        # the answer; each attempt (billed at its own model's rates) is in `iterations`.
        # Kept verbatim for cost tracking (Phase 5).
        out["iterations"] = iterations
    return out


def from_anthropic(msg: dict[str, Any]) -> dict[str, Any]:
    """Anthropic message → OpenAI chat.completion."""
    text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    thinking: list[dict[str, Any]] = []
    for block in msg.get("content", []):
        if block["type"] in ("thinking", "redacted_thinking"):
            thinking.append(block)  # extension: only /v1/messages clients see it
        elif block["type"] == "text":
            text.append(block["text"])
        elif block["type"] == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                }
            )
        # fallback blocks have no OpenAI equivalent.
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thinking:
        message["thinking_blocks"] = thinking
    return {
        "id": msg["id"],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": msg["model"],
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": FINISH_REASON.get(msg.get("stop_reason") or "", "stop"),
            }
        ],
        "usage": usage_to_openai(msg.get("usage") or {}),
    }


class StreamTranslator:
    """Anthropic stream events → OpenAI chat.completion.chunk dicts.

    Anthropic numbers *content blocks* (text, tool_use, thinking…); OpenAI numbers
    *tool calls*. So tool_use blocks are renumbered 0, 1, 2… as they appear.

    `buffer_tools` (for models with server-side refusal fallback): if a model declines
    mid-stream, the API inserts a `fallback` block and the fallback model continues on
    the same stream. Tool calls started before that boundary must be discarded, so tool
    calls are held back and only sent once the message completes. Text is streamed
    as usual — it stays valid across the boundary.
    """

    def __init__(self, include_usage: bool, buffer_tools: bool = False):
        self.include_usage = include_usage
        self.buffer_tools = buffer_tools
        self.id = ""
        self.model = ""
        self.created = int(time.time())
        self.usage: dict[str, Any] = {}
        self.completed = False  # saw message_stop; a stream cut before it is truncated
        self._tool_index: dict[int, int] = {}  # content block index → tool call index
        self._tool_has_args: dict[int, bool] = {}
        self._held: dict[int, dict[str, Any]] = {}  # tool call index → buffered call
        self._thinking: set[int] = set()  # content block indices of thinking blocks

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _tool_chunk(self, call: dict[str, Any]) -> dict[str, Any]:
        return self._chunk({"tool_calls": [call]})

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event["type"]
        if kind == "message_start":
            msg = event["message"]
            self.id, self.model = msg["id"], msg["model"]
            self.usage = dict(msg.get("usage") or {})
            return [self._chunk({"role": "assistant", "content": ""})]

        if kind == "content_block_start":
            block = event["content_block"]
            if block["type"] == "fallback":
                # Declined mid-stream: drop the declined model's tool calls.
                self._tool_index.clear()
                self._tool_has_args.clear()
                self._held.clear()
                return []
            if block["type"] in ("thinking", "redacted_thinking"):
                self._thinking.add(event["index"])
                start = {"type": block["type"]}
                if block["type"] == "redacted_thinking":
                    start["data"] = block.get("data", "")
                return [self._chunk({"thinking": {"index": event["index"], "start": start}})]
            if block["type"] == "tool_use":
                i = self._tool_index[event["index"]] = len(self._tool_index)
                self._tool_has_args[i] = False
                call = {
                    "index": i,
                    "id": block["id"],
                    "type": "function",
                    "function": {"name": block["name"], "arguments": ""},
                }
                if self.buffer_tools:
                    self._held[i] = call
                    return []
                return [self._tool_chunk(call)]
            return []

        if kind == "content_block_delta":
            delta = event["delta"]
            if delta["type"] == "text_delta" and delta["text"]:
                return [self._chunk({"content": delta["text"]})]
            if delta["type"] == "input_json_delta" and event["index"] in self._tool_index:
                i = self._tool_index[event["index"]]
                part = delta.get("partial_json") or ""
                if not part:
                    return []
                self._tool_has_args[i] = True
                if self.buffer_tools:
                    self._held[i]["function"]["arguments"] += part
                    return []
                return [self._tool_chunk({"index": i, "function": {"arguments": part}})]
            if event["index"] in self._thinking:
                if delta["type"] == "thinking_delta" and delta.get("thinking"):
                    t = {"index": event["index"], "thinking": delta["thinking"]}
                    return [self._chunk({"thinking": t})]
                if delta["type"] == "signature_delta" and delta.get("signature"):
                    t = {"index": event["index"], "signature": delta["signature"]}
                    return [self._chunk({"thinking": t})]
            return []

        if kind == "content_block_stop" and event.get("index") in self._tool_index:
            i = self._tool_index[event["index"]]
            if not self._tool_has_args[i]:
                # A no-argument call streams no input_json_delta. OpenAI sends "{}";
                # clients json.loads() the arguments, and "" fails.
                self._tool_has_args[i] = True
                if self.buffer_tools:
                    self._held[i]["function"]["arguments"] = "{}"
                    return []
                return [self._tool_chunk({"index": i, "function": {"arguments": "{}"}})]
            return []

        if kind == "message_delta":
            # message_delta usage is cumulative for output_tokens.
            self.usage.update(
                {k: v for k, v in (event.get("usage") or {}).items() if v is not None}
            )
            reason = (event.get("delta") or {}).get("stop_reason")
            held = [self._tool_chunk(self._held[i]) for i in sorted(self._held)]
            self._held.clear()
            return [*held, self._chunk({}, FINISH_REASON.get(reason or "", "stop"))]

        if kind == "message_stop":
            self.completed = True
            if self.include_usage:
                final = self._chunk({})
                final["choices"] = []
                final["usage"] = usage_to_openai(self.usage)
                return [final]
            return []

        return []  # ping, other block stops, unknown future events
