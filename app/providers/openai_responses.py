"""Chat completions ↔ OpenAI Responses API, ADR 0014.

OpenAI's newest reasoning models (GPT-6 Astra, GPT-6.1 Sol) only call tools through the
Responses API (`POST /v1/responses`). A model configured with `api: responses` is
translated here; everything else in the OpenAI-compatible adapter stays the same.

Pure functions plus a stream translator; the adapter does the transport. Differences
bridged (field names from the official SDK's types):

- Messages become `input` items: role messages with `input_text` / `input_image` parts;
  assistant tool calls are `function_call` items; tool results are `function_call_output`.
- Tools are flat (`{type: function, name, parameters, strict}`); named tool_choice is
  `{type: function, name}`.
- `max_output_tokens`, `reasoning: {effort, mode}`, `text.format` replace max_tokens,
  reasoning_effort and response_format. There is no `stop` or `n`.
- `store: false`: OpenAI would otherwise keep every prompt and answer.
- The answer is a list of output items; usage has `input_tokens` / `output_tokens`
  (cache reads and writes in `input_tokens_details`).
- Streams are named events and end with `response.completed` (or `.incomplete` /
  `.failed`), not `[DONE]`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.providers.base import ProviderError, UnsupportedRequest

FINISH = {"max_output_tokens": "length", "content_filter": "content_filter"}


# --- request ---------------------------------------------------------------


def to_responses(
    request: dict[str, Any], model: str, reasoning_mode: str | None = None
) -> dict[str, Any]:
    """An (already shaped) chat-completions request → a Responses API request body."""
    if request.get("n", 1) != 1:
        raise UnsupportedRequest("n > 1 is not supported by the Responses API")
    out: dict[str, Any] = {
        "model": model,
        "input": _input(request.get("messages") or []),
        "store": False,
    }
    if (cap := request.get("max_completion_tokens") or request.get("max_tokens")) is not None:
        out["max_output_tokens"] = int(cap)
    for name in ("temperature", "top_p", "parallel_tool_calls"):
        if request.get(name) is not None:
            out[name] = request[name]
    if user := request.get("user"):
        out["safety_identifier"] = str(user)
    reasoning: dict[str, Any] = {}
    if effort := request.get("reasoning_effort"):
        reasoning["effort"] = effort
    if reasoning_mode:
        reasoning["mode"] = (
            reasoning_mode  # e.g. "pro": a configured model, not the client's choice
        )
    if reasoning:
        out["reasoning"] = reasoning
    if tools := request.get("tools"):
        out["tools"] = [_tool(t) for t in tools]
    if (choice := request.get("tool_choice")) is not None:
        out["tool_choice"] = _tool_choice(choice)
    fmt = request.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        spec = fmt.get("json_schema") or {}
        out["text"] = {
            "format": {
                "type": "json_schema",
                "name": spec.get("name") or "response",
                "schema": spec.get("schema") or {},
                **({"strict": spec["strict"]} if "strict" in spec else {}),
            }
        }
    elif fmt.get("type") == "json_object":
        out["text"] = {"format": {"type": "json_object"}}
    return out


def _input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg["tool_call_id"],
                    "output": _text(msg.get("content")),
                }
            )
        elif role == "assistant":
            if text := _text(msg.get("content")):
                items.append({"role": "assistant", "content": text})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
        elif role in ("system", "developer", "user"):
            items.append({"role": role, "content": _parts(msg.get("content"))})
        else:
            raise UnsupportedRequest(f"message role {role!r} is not supported by the Responses API")
    return items


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
    )


def _parts(content: Any) -> str | list[dict[str, Any]]:
    if content is None or isinstance(content, str):
        return content or ""
    parts: list[dict[str, Any]] = []
    for p in content:
        kind = p.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": p.get("text", "")})
        elif kind == "image_url":
            image = p.get("image_url") or {}
            parts.append(
                {
                    "type": "input_image",
                    "image_url": image.get("url"),
                    "detail": image.get("detail") or "auto",
                }
            )
        else:
            raise UnsupportedRequest(
                f"content part type {kind!r} is not supported by the Responses API"
            )
    return parts


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") != "function":
        raise UnsupportedRequest(f"tool type {tool.get('type')!r} is not supported")
    fn = tool.get("function") or {}
    out: dict[str, Any] = {
        "type": "function",
        "name": fn["name"],
        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        # Responses defaults to strict when `strict` is omitted; chat completions doesn't.
        "strict": bool(fn.get("strict")),
    }
    if desc := fn.get("description"):
        out["description"] = desc
    return out


def _tool_choice(choice: Any) -> Any:
    if choice in ("auto", "none", "required"):
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function":
        return {"type": "function", "name": (choice.get("function") or {}).get("name")}
    raise UnsupportedRequest(f"tool_choice {choice!r} is not supported")


# --- response --------------------------------------------------------------


def usage_to_chat(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    details = usage.get("input_tokens_details") or {}
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {
            "cached_tokens": int(details.get("cached_tokens") or 0),
            # billed at cache_write (ADR 0015); the same extension field Anthropic uses
            "cache_creation_tokens": int(details.get("cache_write_tokens") or 0),
        },
    }
    if reasoning := (usage.get("output_tokens_details") or {}).get("reasoning_tokens"):
        out["completion_tokens_details"] = {"reasoning_tokens": int(reasoning)}
    return out


def _finish(response: dict[str, Any], has_calls: bool) -> str:
    if response.get("status") == "incomplete":
        return FINISH.get((response.get("incomplete_details") or {}).get("reason") or "", "length")
    return "tool_calls" if has_calls else "stop"


def failure(provider: str, response: dict[str, Any]) -> ProviderError:
    err = response.get("error") or {}
    return ProviderError(
        f"{provider} failed to generate a response", retryable=True, detail=json.dumps(err)[:500]
    )


def from_responses(provider: str, response: dict[str, Any]) -> dict[str, Any]:
    """A Responses API response object → chat.completion."""
    if response.get("status") == "failed":
        raise failure(provider, response)
    text, refusal, calls = [], [], []
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    refusal.append(part.get("refusal", ""))
        elif kind == "function_call":
            calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
        # reasoning items: summaries only, no chat equivalent
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if refusal:
        message["refusal"] = "".join(refusal)
    if calls:
        message["tool_calls"] = calls
    return {
        "id": response.get("id", ""),
        "object": "chat.completion",
        "created": int(response.get("created_at") or time.time()),
        "model": response.get("model", ""),
        "choices": [
            {"index": 0, "message": message, "finish_reason": _finish(response, bool(calls))}
        ],
        "usage": usage_to_chat(response.get("usage")),
    }


class StreamTranslator:
    """Responses API stream events → chat.completion.chunk dicts.

    Function calls are numbered 0, 1, 2… as their output items appear; argument deltas
    find their call by `item_id` (or `output_index`). `completed` is set by the terminal
    event; a stream that ends without one was cut off.
    """

    def __init__(self, provider: str, include_usage: bool) -> None:
        self.provider = provider
        self.include_usage = include_usage
        self.id, self.model, self.created = "", "", int(time.time())
        self.completed = False
        self._calls: dict[str, int] = {}  # item_id / "#output_index" → tool call index

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _call_index(self, event: dict[str, Any]) -> int | None:
        for key in (event.get("item_id"), f"#{event.get('output_index')}"):
            if key in self._calls:
                return self._calls[key]
        return None

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "response.created":
            response = event.get("response") or {}
            self.id, self.model = response.get("id", ""), response.get("model", "")
            return [self._chunk({"role": "assistant", "content": ""})]
        if kind == "response.output_text.delta" and event.get("delta"):
            return [self._chunk({"content": event["delta"]})]
        if kind == "response.refusal.delta" and event.get("delta"):
            return [self._chunk({"refusal": event["delta"]})]
        if kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return []
            i = len(set(self._calls.values()))
            if item.get("id"):
                self._calls[item["id"]] = i
            self._calls[f"#{event.get('output_index')}"] = i
            call = {
                "index": i,
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments") or "",
                },
            }
            return [self._chunk({"tool_calls": [call]})]
        if kind == "response.function_call_arguments.delta" and event.get("delta"):
            idx = self._call_index(event)
            if idx is None:
                return []
            return [
                self._chunk(
                    {"tool_calls": [{"index": idx, "function": {"arguments": event["delta"]}}]}
                )
            ]
        if kind in ("response.completed", "response.incomplete"):
            self.completed = True
            response = event.get("response") or {}
            out = [self._chunk({}, _finish(response, bool(self._calls)))]
            if self.include_usage:
                final = self._chunk({})
                final["choices"] = []
                final["usage"] = usage_to_chat(response.get("usage"))
                out.append(final)
            return out
        if kind == "response.failed":
            raise failure(self.provider, event.get("response") or {})
        if kind == "error":
            raise ProviderError(
                f"{self.provider} failed mid-stream", retryable=True, detail=json.dumps(event)[:500]
            )
        return []  # in_progress, content_part.*, reasoning summaries, *.done, …
