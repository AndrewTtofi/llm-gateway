"""Gateway entrypoint: health, model listing, config reload, chat completions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import signal
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from redis.asyncio import Redis

from app import config, providers
from app.providers import UnsupportedProvider
from app.providers.base import (
    CLIENT_FAULT_STATUS,
    QUOTA_CODES,
    ProviderAdapter,
    ProviderError,
    UnsupportedRequest,
)
from app.routing import router
from app.routing.breaker import BreakerStore, MemoryBreakerStore, RedisBreakerStore
from app.routing.router import AllTargetsFailed, CommittedStream, UnknownModel
from app.schemas import ChatCompletionRequest

log = logging.getLogger(__name__)


def reload_from_signal() -> None:
    try:
        reg = config.reload_registry()
        log.warning("config reloaded on SIGHUP: %d aliases", len(reg.aliases))
    except Exception:
        log.exception("config reload on SIGHUP failed; keeping the previous config")


def make_breaker_store() -> tuple[BreakerStore, Redis | None]:
    if config.registry.circuit_breaker.store == "memory":
        return MemoryBreakerStore(), None
    timeout = config.registry.circuit_breaker.redis_timeout_ms / 1000
    redis = Redis.from_url(
        config.settings.redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
    )
    return RedisBreakerStore(redis), redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    router.store, redis = make_breaker_store()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGHUP, reload_from_signal)  # `kill -HUP <pid>`
    yield
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.remove_signal_handler(signal.SIGHUP)
    await providers.pool.aclose()
    if redis is not None:
        await redis.aclose()


app = FastAPI(title="LLM Gateway", version="0.3.0", lifespan=lifespan)


def error_response(
    status: int,
    message: str,
    type_: str = "invalid_request_error",
    code: str | int | None = None,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """OpenAI's error shape, so SDK clients raise the right exception class."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "param": param, "code": code}},
        headers=headers,
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return error_response(
        exc.status_code, str(exc.detail), code=exc.status_code, headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    param = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or None
    return error_response(400, f"Invalid request: {first.get('msg', 'bad body')}", param=param)


def provider_error_response(exc: ProviderError) -> JSONResponse:
    """Map an upstream failure to what the client should see.

    Client-caused upstream 4xx (bad params, context too long) pass through. Anything that
    is the gateway's problem — provider auth, wrong configured model (404), exhausted
    quota, provider outage — is a 5xx, so clients don't "fix" a request that was fine.
    """
    status = exc.status
    if exc.timeout:
        return error_response(504, exc.message, "api_error", "upstream_timeout")
    if exc.code in QUOTA_CODES:
        # Not retryable: waiting won't help, so no retry-after and not a 429.
        return error_response(503, exc.message, "api_error", "upstream_quota_exhausted")
    if status == 429:
        return error_response(
            429, exc.message, "rate_limit_error", "upstream_rate_limited", headers=exc.headers
        )
    if status in CLIENT_FAULT_STATUS:
        return error_response(status, exc.message, "invalid_request_error", "upstream_rejected")
    return error_response(502, exc.message, "api_error", "upstream_error")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict[str, object]:
    """Aliases first (with their fallback chain), then every provider/model they use."""
    reg = config.registry
    data: list[dict[str, object]] = [
        {"id": name, "object": "model", "created": 0, "owned_by": "gateway", "chain": a.chain}
        for name, a in reg.aliases.items()
    ]
    if reg.allow_direct_models:
        direct = dict.fromkeys(m for a in reg.aliases.values() for m in a.chain)
        data += [
            {"id": m, "object": "model", "created": 0, "owned_by": m.split("/", 1)[0]}
            for m in direct
        ]
    return {"object": "list", "data": data}


@app.post("/admin/reload", response_model=None)
async def reload(authorization: str = Header(default="")) -> dict[str, object] | JSONResponse:
    key = config.settings.gateway_admin_key
    if not key or not secrets.compare_digest(authorization, f"Bearer {key}"):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        reg = config.reload_registry()
    except Exception as exc:  # bad YAML / validation error: the old config stays live
        msg = f"reload failed, previous config kept: {type(exc).__name__}: {str(exc)[:300]}"
        return error_response(400, msg, code="invalid_config")
    return {"reloaded": True, "aliases": list(reg.aliases)}


@app.get("/admin/providers", response_model=None)
async def provider_status(authorization: str = Header(default="")) -> dict[str, object]:
    """Circuit-breaker state of every target used by an alias."""
    key = config.settings.gateway_admin_key
    if not key or not secrets.compare_digest(authorization, f"Bearer {key}"):
        raise HTTPException(status_code=401, detail="unauthorized")
    targets = dict.fromkeys(t for a in config.registry.aliases.values() for t in a.chain)
    return {"targets": {t: str(await router.store.state(t)) for t in targets}}


def resolve_target(model: str) -> tuple[ProviderAdapter, str, str]:
    """alias or provider/model → (adapter, upstream model id, "provider/model") of the
    chain's first entry. Routing uses the whole chain (app.routing.router); this is for
    tools and tests that need one specific adapter."""
    reg = config.registry
    try:
        target = reg.resolve(model)[0]
    except KeyError as exc:
        raise LookupError(f"The model '{model}' does not exist") from exc
    provider, _, upstream_model = target.partition("/")
    cfg = reg.providers.get(provider)
    if cfg is None:
        raise LookupError(f"The model '{model}' does not exist")
    return providers.pool.get(provider, cfg), upstream_model, target


class ClientDisconnected(Exception):
    pass


async def cancel_on_disconnect[T](request: Request, work: Awaitable[T]) -> T:
    """Run `work`, cancelling it if the client hangs up (→ ClientDisconnected).

    After the body is read, ASGI `receive()` blocks until the client disconnects,
    so a watcher task waiting on it is a cheap disconnect signal.
    """
    task = asyncio.ensure_future(work)
    receive: Callable[[], Awaitable[Any]] = request.receive

    async def watch() -> None:
        while (await receive())["type"] != "http.disconnect":
            pass
        task.cancel()

    watcher = asyncio.create_task(watch())
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise  # we are being cancelled ourselves (e.g. shutdown) — never swallow that
        raise ClientDisconnected from None
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher


def routing_error_response(exc: AllTargetsFailed) -> JSONResponse:
    headers = exc.routed.headers()
    if exc.all_open:
        return error_response(
            503,
            "all providers for this model are temporarily unavailable",
            "api_error",
            "all_providers_unavailable",
            headers=headers,
        )
    last = exc.last
    if isinstance(last, UnsupportedRequest):
        return error_response(400, str(last), code="unsupported_parameter", headers=headers)
    if isinstance(last, UnsupportedProvider):
        return error_response(
            501, str(last), "api_error", "provider_not_supported", headers=headers
        )
    if isinstance(last, ProviderError):
        resp = provider_error_response(last)
        resp.headers.update(headers)
        return resp
    return error_response(
        503,
        "no provider could serve the request",
        "api_error",
        "all_providers_unavailable",
        headers=headers,
    )


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest, request: Request
) -> JSONResponse | StreamingResponse | Response:
    def unknown() -> JSONResponse:
        return error_response(
            404, f"The model '{body.model}' does not exist", code="model_not_found", param="model"
        )

    if not body.stream:
        try:
            result, routed = await cancel_on_disconnect(request, router.route_chat(body))
        except UnknownModel:
            return unknown()
        except AllTargetsFailed as exc:
            return routing_error_response(exc)
        except ClientDisconnected:
            return Response(status_code=499)  # nobody left to answer
        return JSONResponse(result, headers=routed.headers())

    # Streaming. Retries and fallback cover everything up to the first chunk, which is
    # pulled *before* the 200 goes out (ADR 0002, 0005). That wait can be long (prompt
    # processing), so it is watched for disconnects too.
    try:
        (first, chunks), routed = await cancel_on_disconnect(request, router.route_stream(body))
    except UnknownModel:
        return unknown()
    except AllTargetsFailed as exc:
        return routing_error_response(exc)
    except ClientDisconnected:
        return Response(status_code=499)

    return SSEResponse(
        relay_sse(first, chunks),
        upstream=chunks,
        headers={**routed.headers(), "cache-control": "no-cache", "x-accel-buffering": "no"},
    )


class SSEResponse(StreamingResponse):
    """StreamingResponse that always closes the upstream stream when the response ends.

    Starlette cancels a response on disconnect but never closes its body iterator. If
    the cancel lands while we're suspended at a `yield` (slow client, or before the body
    started), nothing would close the upstream until garbage collection — and the
    provider would keep generating, and billing, tokens.
    """

    def __init__(
        self,
        content: AsyncGenerator[str],
        upstream: CommittedStream | AsyncGenerator[dict[str, Any]],
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(content, media_type="text/event-stream", headers=headers)
        self._content = content
        self._upstream = upstream

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Our task may already be cancelled; shield so the cleanup awaits still run.
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(RuntimeError):  # already running/closed
                    await self._content.aclose()
                with contextlib.suppress(RuntimeError):
                    await self._upstream.aclose()


def sse_error(message: str) -> str:
    err = {
        "error": {"message": message, "type": "api_error", "param": None, "code": "upstream_error"}
    }
    return f"data: {json.dumps(err)}\n\n"


async def relay_sse(
    first: dict[str, Any] | None, chunks: AsyncIterator[dict[str, Any]]
) -> AsyncGenerator[str]:
    """Re-emit upstream chunks as SSE. Once the 200 is sent, errors can only travel in-band.

    An errored stream ends with an error event and no `[DONE]`, so it can't be mistaken
    for a complete answer. Upstream cleanup is SSEResponse's job.
    """
    try:
        if first is not None:
            yield f"data: {json.dumps(first)}\n\n"
        async for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
    except ProviderError as exc:
        yield sse_error(exc.message)
        return
    except Exception:
        log.exception("stream relay failed")
        yield sse_error("gateway error while streaming")
        return
    yield "data: [DONE]\n\n"
