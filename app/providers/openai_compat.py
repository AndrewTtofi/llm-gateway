"""Adapter for OpenAI and any OpenAI-compatible API (Ollama, vLLM, Groq, OpenRouter, …).

The internal format *is* OpenAI's, so this adapter only handles transport:
auth, timeouts, SSE parsing and error mapping.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.providers.base import (
    CLIENT_FAULT_STATUS,
    RETRYABLE_STATUS,
    ProviderAdapter,
    ProviderError,
)


class OpenAICompatAdapter(ProviderAdapter):
    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        t = cfg.get("timeouts", {})
        connect = float(t.get("connect", 5))
        # Non-streaming: the provider sends nothing until the whole answer is generated,
        # so the read timeout has to cover the full generation → `total`.
        self._timeout = httpx.Timeout(float(t.get("total", 300)), connect=connect)
        # Streaming: read timeout is the max silence between chunks. The longest silence
        # is before the first token (prompt processing) → `first_token`.
        self._stream_timeout = httpx.Timeout(float(t.get("first_token", 30)), connect=connect)
        headers = {}
        key_env = cfg.get("api_key_env")
        if key_env and (key := os.environ.get(key_env)):
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.AsyncClient(base_url=str(cfg.get("base_url", "")), headers=headers)

    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        body = {**request, "model": model, "stream": False}
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
        body = {**request, "model": model, "stream": True}
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=body, timeout=self._stream_timeout
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _status_error(self.name, resp.status_code, resp.text, resp.headers)
                async for chunk in _parse_sse(self.name, resp):
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


def _status_error(provider: str, status: int, text: str, headers: httpx.Headers) -> ProviderError:
    try:
        err = json.loads(text).get("error", {})
    except ValueError, AttributeError:
        err = {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    detail = str(err.get("message") or text)[:500]
    code = str(err["code"]) if err.get("code") else None

    if status in CLIENT_FAULT_STATUS:
        message = f"{provider} rejected the request: {detail}"
    elif status in (401, 403):
        message = f"{provider} rejected the gateway's credentials (gateway configuration)"
    elif status == 404:
        message = (
            f"{provider} does not know the configured model or endpoint (gateway configuration)"
        )
    elif status == 429 and code == "insufficient_quota":
        message = f"{provider} quota exhausted"
    elif status == 429:
        message = f"{provider} rate limited the gateway"
    else:
        message = f"{provider} returned HTTP {status}"

    keep = {k: v for k, v in headers.items() if k.lower() == "retry-after"}
    return ProviderError(
        message,
        status=status,
        retryable=status in RETRYABLE_STATUS and code != "insufficient_quota",
        headers=keep,
        code=code,
        detail=detail,
    )


def _invalid_response(provider: str, text: str) -> ProviderError:
    return ProviderError(
        f"{provider} returned an invalid response", retryable=True, detail=text[:500]
    )


def _network_error(provider: str, exc: httpx.HTTPError) -> ProviderError:
    timeout = isinstance(exc, httpx.TimeoutException)
    kind = "timed out" if timeout else f"connection failed ({type(exc).__name__})"
    return ProviderError(f"{provider} {kind}", retryable=True, timeout=timeout)
