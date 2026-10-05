"""SSE relay, disconnect handling and upstream cleanup (ADR 0002, 0005)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.providers.base import ProviderError

log = logging.getLogger(__name__)


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


class Closable(Protocol):
    async def aclose(self) -> None: ...


class ChunkFilter(Protocol):
    def observe(self, chunk: dict[str, Any]) -> dict[str, Any] | None: ...
    def failed(self, code: str) -> None: ...
    def finished(self) -> None: ...


class SSEResponse(StreamingResponse):
    """StreamingResponse that always closes the upstream stream when the response ends,
    then runs `on_close` (usage accounting).

    Starlette cancels a response on disconnect but never closes its body iterator. If
    the cancel lands while we're suspended at a `yield` (slow client, or before the body
    started), nothing would close the upstream until garbage collection — and the
    provider would keep generating, and billing, tokens.
    """

    def __init__(
        self,
        content: AsyncGenerator[str],
        upstream: Closable,
        headers: Mapping[str, str],
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(content, media_type="text/event-stream", headers=headers)
        self._content = content
        self._upstream = upstream
        self._on_close = on_close

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
                if self._on_close is not None:
                    try:
                        await self._on_close()
                    except Exception:
                        log.exception("stream close hook failed")


def sse_error(message: str) -> str:
    err = {
        "error": {"message": message, "type": "api_error", "param": None, "code": "upstream_error"}
    }
    return f"data: {json.dumps(err)}\n\n"


async def relay_sse(
    first: dict[str, Any] | None,
    chunks: AsyncIterator[dict[str, Any]],
    meter: ChunkFilter | None = None,
) -> AsyncGenerator[str]:
    """Re-emit upstream chunks as SSE. Once the 200 is sent, errors can only travel in-band.

    `meter` sees every chunk (to count usage) and may drop or rewrite it — e.g. the usage
    chunk the gateway asked for but the client didn't. An errored stream ends with an
    error event and no `[DONE]`, so it can't be mistaken for a complete answer.
    """

    def emit(chunk: dict[str, Any]) -> str | None:
        out = meter.observe(chunk) if meter is not None else chunk
        return f"data: {json.dumps(out)}\n\n" if out is not None else None

    try:
        if first is not None and (line := emit(first)):
            yield line
        async for chunk in chunks:
            if line := emit(chunk):
                yield line
    except ProviderError as exc:
        if meter is not None:
            meter.failed("upstream_timeout" if exc.timeout else "upstream_error")
        yield sse_error(exc.message)
        return
    except Exception:
        log.exception("stream relay failed")
        if meter is not None:
            meter.failed("gateway_error")
        yield sse_error("gateway error while streaming")
        return
    if meter is not None:
        meter.finished()  # everything delivered; a disconnect from here on isn't a loss
    yield "data: [DONE]\n\n"
