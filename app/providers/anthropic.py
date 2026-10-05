"""Adapter for Anthropic's Messages API, via the official `anthropic` SDK.

The SDK handles auth headers, API versioning, SSE parsing and typed errors; this
adapter only translates (see `anthropic_format.py`) and maps errors. SDK retries are
off — retry and fallback policy belongs to the gateway (Phase 3).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any

import anthropic
import httpx2

from app.providers.anthropic_format import (
    DEFAULT_MAX_TOKENS,
    StreamTranslator,
    caps_for,
    from_anthropic,
    to_anthropic,
)
from app.providers.base import (
    ProviderAdapter,
    ProviderError,
    first_then_rest,
    upstream_status_error,
)

# Server-side refusal fallback: if a safety classifier declines, the API re-runs the
# request on a fallback model inside the same call (beta).
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAdapter(ProviderAdapter):
    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        t = cfg.get("timeouts", {})
        connect = float(t.get("connect", 5))
        # Same reasoning as the OpenAI adapter: non-streaming reads must cover the whole
        # generation; streaming reads only the longest gap (before the first token).
        pool_wait = float(t.get("pool", 5))  # fail fast when all connections are busy
        self._timeout = httpx2.Timeout(float(t.get("total", 300)), connect=connect, pool=pool_wait)
        self._stream_timeout = httpx2.Timeout(
            float(t.get("stream_idle", 300)), connect=connect, pool=pool_wait
        )
        self._first_token = float(t.get("first_token", 30))
        self._default_max_tokens = int(cfg.get("default_max_tokens", DEFAULT_MAX_TOKENS))
        key_env = cfg.get("api_key_env")
        self._key_env = str(key_env or "")
        key = os.environ.get(self._key_env) if key_env else None
        lim = cfg.get("limits", {})
        limits = httpx2.Limits(
            max_connections=int(lim.get("max_connections", 100)),
            max_keepalive_connections=int(lim.get("max_keepalive", 20)),
        )
        self._client: anthropic.AsyncAnthropic | None = (
            anthropic.AsyncAnthropic(
                api_key=key,
                base_url=cfg.get("base_url") or None,
                max_retries=0,
                # The SDK's own client class keeps its defaults; only the pool changes.
                http_client=anthropic.DefaultAsyncHttpxClient(limits=limits),
            )
            if key
            else None
        )

    @property
    def configured(self) -> bool:
        return self._client is not None

    def _require_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            raise ProviderError(
                f"{self.name} is not configured ({self._key_env} is not set)", status=None
            )
        return self._client

    def _params(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        caps = caps_for(self.cfg, model)
        params = to_anthropic(request, model, caps, self._default_max_tokens)
        if caps.get("refusal_fallback"):
            params["betas"] = [REFUSAL_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _messages(self, params: dict[str, Any]) -> Any:
        client = self._require_client()
        return client.beta.messages if "betas" in params else client.messages

    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        params = self._params(model, request)
        try:
            msg = await self._messages(params).create(**params, timeout=self._timeout)
        # Non-streamed, the read timeout is `total`: the whole answer took too long.
        except anthropic.APIError as exc:
            raise _map_error(self.name, exc, deadline=True) from exc
        except httpx2.HTTPError as exc:
            raise _transport_error(self.name, exc, deadline=True) from exc
        return from_anthropic(msg.model_dump(exclude_none=True))

    async def stream(self, model: str, request: dict[str, Any]) -> AsyncGenerator[dict[str, Any]]:
        params = self._params(model, request)
        opts = request.get("stream_options") or {}
        translator = StreamTranslator(
            include_usage=bool(opts.get("include_usage")),
            buffer_tools="fallbacks" in params,
        )
        try:
            async with asyncio.timeout(self._first_token):
                events = await self._messages(params).create(
                    **params, stream=True, timeout=self._stream_timeout
                )
            async with events:  # closes the HTTP response on exit/cancel
                async for event in first_then_rest(events, self._first_token, self.name):
                    for chunk in translator.feed(event.model_dump(exclude_none=True)):
                        yield chunk
        except anthropic.APIError as exc:
            raise _map_error(self.name, exc) from exc
        except httpx2.HTTPError as exc:  # raised unwrapped while reading the stream
            raise _transport_error(self.name, exc) from exc
        except TimeoutError as exc:
            raise ProviderError(
                f"{self.name} timed out waiting for the first token", retryable=True, timeout=True
            ) from exc
        if not translator.completed:
            # The connection closed cleanly mid-answer (proxy, LB). Without this the
            # client would get [DONE] and take a truncated answer as complete.
            raise ProviderError(f"{self.name} stream ended early", retryable=True)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _transport_error(
    provider: str, exc: BaseException | None, deadline: bool = False
) -> ProviderError:
    """Pool full: the request never left (ADR 0023). Connect timeout: nothing was sent, a
    network failure. Other timeouts: the provider was working on it."""
    if isinstance(exc, httpx2.PoolTimeout):
        return ProviderError(f"{provider} is busy (no free connection)", retryable=True, local=True)
    if isinstance(exc, httpx2.TimeoutException) and not isinstance(exc, httpx2.ConnectTimeout):
        return ProviderError(
            f"{provider} timed out", retryable=True, timeout=True, deadline=deadline
        )
    return ProviderError(f"{provider} connection failed ({type(exc).__name__})", retryable=True)


def _map_error(provider: str, exc: anthropic.APIError, deadline: bool = False) -> ProviderError:
    if isinstance(exc, anthropic.APITimeoutError):
        # The SDK wraps the transport's timeout; its cause says which one.
        cause = exc.__cause__
        if isinstance(cause, httpx2.TimeoutException):
            return _transport_error(provider, cause, deadline)
        return ProviderError(
            f"{provider} timed out", retryable=True, timeout=True, deadline=deadline
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(f"{provider} connection failed", retryable=True)
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error", body) if isinstance(body, dict) else {}
    err = err if isinstance(err, dict) else {}
    detail = str(err.get("message") or exc.message)
    code = str(err["type"]) if err.get("type") else None
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 400:
        return upstream_status_error(provider, exc.status_code, detail, code, exc.response.headers)
    # An `error` event after the stream started (e.g. overloaded_error). The SDK raises it
    # as an APIStatusError carrying the stream's original status — 200.
    return ProviderError(f"{provider} failed mid-stream", retryable=True, code=code, detail=detail)
