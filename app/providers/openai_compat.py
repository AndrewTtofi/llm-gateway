"""Adapter for OpenAI and any OpenAI-compatible API (Ollama, vLLM, Groq, OpenRouter, …).

The internal format *is* OpenAI's, so this adapter mostly handles transport: auth,
timeouts, SSE parsing and error mapping. "OpenAI-compatible" APIs still differ in what
they accept, so each provider (and model) can declare it in config (ADR 0012):

    params:
      allow: [...]        # only these request fields are sent (strict APIs reject the rest)
      drop: [...]         # never sent
      rename: {a: b}      # sent under another name (max_tokens → max_completion_tokens)
      values: {k: [...]}  # field sent only with one of these values (reasoning_effort)
    tools: false          # model can't call tools on this API → try the next target
    vision: false         # model doesn't take images → try the next target

A provider rejecting a parameter returns 400, which the router treats as the client's
fault and does *not* fall back on, so these rules matter: they keep a fallback chain
working across providers that disagree about parameters.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.providers.base import (
    ProviderAdapter,
    ProviderError,
    UnsupportedRequest,
    first_then_rest,
    upstream_status_error,
)

# Always sent: the request is meaningless without them.
ESSENTIAL = frozenset({"model", "messages", "stream", "stream_options"})


def rules_for(cfg: dict[str, Any], model: str) -> dict[str, Any]:
    """Provider `params`/`tools`/`vision`, overlaid with `models.<model>`'s."""
    base = cfg.get("params") or {}
    spec = (cfg.get("models") or {}).get(model) or {}
    own = spec.get("params") or {}
    return {
        "allow": set(own.get("allow") or base.get("allow") or ()),
        "drop": set(base.get("drop") or ()) | set(own.get("drop") or ()),
        "rename": {**(base.get("rename") or {}), **(own.get("rename") or {})},
        "values": {**(base.get("values") or {}), **(own.get("values") or {})},
        "tools": spec.get("tools", cfg.get("tools", True)),
        "vision": spec.get("vision", cfg.get("vision", True)),
    }


def _has_images(request: dict[str, Any]) -> bool:
    for msg in request.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            return True
    return False


def shape_request(
    name: str, cfg: dict[str, Any], model: str, request: dict[str, Any]
) -> dict[str, Any]:
    """Fit an OpenAI-format request to what this provider/model accepts."""
    rules = rules_for(cfg, model)
    if request.get("tools") and not rules["tools"]:
        raise UnsupportedRequest(f"{name}/{model} can't call tools through this API")
    if not rules["vision"] and _has_images(request):
        raise UnsupportedRequest(f"{name}/{model} doesn't accept images")
    body = dict(request)
    for old, new in rules["rename"].items():
        if old in body:
            value = body.pop(old)
            body.setdefault(new, value)  # if the client sent both, the new name wins
    for field in rules["drop"]:
        body.pop(field, None)
    if rules["allow"]:
        body = {k: v for k, v in body.items() if k in rules["allow"] or k in ESSENTIAL}
    for field, allowed in rules["values"].items():
        if field in body and body[field] not in allowed:
            del body[field]  # an unsupported value: let the provider use its default
    return body


class OpenAICompatAdapter(ProviderAdapter):
    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        t = cfg.get("timeouts", {})
        connect = float(t.get("connect", 5))
        # Non-streaming: the provider sends nothing until the whole answer is generated,
        # so the read timeout has to cover the full generation → `total`.
        pool_wait = float(t.get("pool", 5))
        # pool: how long to wait for a free connection when all are busy. Short, so a
        # saturated provider fails fast and the router falls back instead of queueing.
        self._timeout = httpx.Timeout(float(t.get("total", 300)), connect=connect, pool=pool_wait)
        # Streaming: the read timeout bounds silence between chunks (`stream_idle`); the
        # wait for the first chunk (prompt processing) has its own `first_token` budget.
        self._stream_timeout = httpx.Timeout(
            float(t.get("stream_idle", 120)), connect=connect, pool=pool_wait
        )
        self._first_token = float(t.get("first_token", 30))
        headers = {}
        key_env = cfg.get("api_key_env")
        self._key_env = key_env
        self._configured = True
        if key_env:
            if key := os.environ.get(key_env):
                headers["Authorization"] = f"Bearer {key}"
            else:
                self._configured = False  # fail fast below instead of a certain 401
        lim = cfg.get("limits", {})
        limits = httpx.Limits(
            max_connections=int(lim.get("max_connections", 100)),
            max_keepalive_connections=int(lim.get("max_keepalive", 20)),
        )
        self._client = httpx.AsyncClient(
            base_url=str(cfg.get("base_url", "")), headers=headers, limits=limits
        )

    def _require_key(self) -> None:
        """A provider whose key isn't set can't serve: fall back without a network call
        (a gateway fault, like the Anthropic adapter's)."""
        if not self._configured:
            raise ProviderError(
                f"{self.name} is not configured ({self._key_env} is not set)", status=None
            )

    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        self._require_key()
        body = {
            **shape_request(self.name, self.cfg, model, request),
            "model": model,
            "stream": False,
        }
        try:
            resp = await self._client.post("/chat/completions", json=body, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise _network_error(self.name, exc) from exc
        if resp.status_code >= 400:
            raise _status_error(self.name, resp.status_code, resp.text, resp.headers)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):  # e.g. an HTML page from a proxy in between
            raise _invalid_response(self.name, resp.text)
        return data

    async def stream(self, model: str, request: dict[str, Any]) -> AsyncGenerator[dict[str, Any]]:
        self._require_key()
        body = {
            **shape_request(self.name, self.cfg, model, request),
            "model": model,
            "stream": True,
        }
        if self.cfg.get("stream_usage") is False:  # provider rejects stream_options
            body.pop("stream_options", None)
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=body, timeout=self._stream_timeout
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _status_error(self.name, resp.status_code, resp.text, resp.headers)
                chunks = _parse_sse(self.name, resp)
                async for chunk in first_then_rest(chunks, self._first_token, self.name):
                    yield chunk
        except httpx.HTTPError as exc:
            raise _network_error(self.name, exc) from exc

    async def aclose(self) -> None:
        await self._client.aclose()


async def _parse_sse(provider: str, resp: httpx.Response) -> AsyncGenerator[dict[str, Any]]:
    """Server-Sent Events: `data: <json>` lines separated by blank lines, ending in `data: [DONE]`.

    Comment lines (`: keep-alive`) and other fields (`event:`, `id:`) are ignored —
    OpenAI-style chat streams put everything in `data`.
    """
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            chunk = None
        if not isinstance(chunk, dict):
            raise _invalid_response(provider, payload)
        if "error" in chunk:  # provider failed after the 200 was already sent
            raise ProviderError(
                f"{provider} failed mid-stream", retryable=True, detail=json.dumps(chunk["error"])
            )
        yield chunk
    # No [DONE]: the connection closed mid-answer. Don't let it pass as complete.
    raise ProviderError(f"{provider} stream ended early", retryable=True)


def _status_error(provider: str, status: int, text: str, headers: httpx.Headers) -> ProviderError:
    try:
        err = json.loads(text).get("error", {})
    except ValueError, AttributeError:
        err = {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    detail = str(err.get("message") or text)
    code = str(err["code"]) if err.get("code") else None
    return upstream_status_error(provider, status, detail, code, headers)


def _invalid_response(provider: str, text: str) -> ProviderError:
    return ProviderError(
        f"{provider} returned an invalid response", retryable=True, detail=text[:500]
    )


def _network_error(provider: str, exc: httpx.HTTPError) -> ProviderError:
    timeout = isinstance(exc, httpx.TimeoutException)
    kind = "timed out" if timeout else f"connection failed ({type(exc).__name__})"
    return ProviderError(f"{provider} {kind}", retryable=True, timeout=timeout)
