"""Client disconnect must cancel the upstream call, so the provider stops generating.

These drive the app at the ASGI level so the test controls exactly when the
client "hangs up". The upstream is an httpx MockTransport whose response never
finishes, and records whether the gateway closed it.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app import main
from tests.conftest import UPSTREAM


class HangingStream(httpx.AsyncByteStream):
    """Sends one SSE chunk, then blocks forever (a model that's still generating)."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"choices": [{"index": 0, "delta": {"content": "a"}}]}\n\n'
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


def use_transport(handler: Any) -> None:
    """Point the mock provider's adapter at an in-memory transport."""
    adapter, _, _ = main.resolve_target("local")
    adapter._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=UPSTREAM, transport=httpx.MockTransport(handler)
    )


async def call_app(payload: dict[str, Any], hang_up: asyncio.Event) -> list[dict[str, Any]]:
    """Run one request through the ASGI app; the client disconnects when `hang_up` is set."""
    sent: list[dict[str, Any]] = []
    inbox = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]

    async def receive() -> dict[str, Any]:
        if inbox:
            return inbox.pop(0)
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},  # what uvicorn sends
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await asyncio.wait_for(main.app(scope, receive, send), timeout=5)
    return sent


@pytest.mark.usefixtures("registry")
async def test_stream_disconnect_closes_upstream() -> None:
    upstream = HangingStream()
    use_transport(lambda req: httpx.Response(200, stream=upstream))
    hang_up = asyncio.Event()

    task = asyncio.create_task(
        call_app(
            {"model": "local", "messages": [{"role": "user", "content": "x"}], "stream": True},
            hang_up,
        )
    )
    await asyncio.sleep(0.2)  # first chunk relayed, upstream now blocked
    hang_up.set()
    sent = await task

    assert any(m.get("body", b"").startswith(b"data: ") for m in sent)  # stream did start
    await asyncio.wait_for(upstream.closed.wait(), timeout=2)  # and upstream got closed


@pytest.mark.usefixtures("registry")
async def test_non_stream_disconnect_cancels_upstream() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_provider(req: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()  # generating a long answer…
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    use_transport(slow_provider)
    hang_up = asyncio.Event()
    task = asyncio.create_task(
        call_app({"model": "local", "messages": [{"role": "user", "content": "x"}]}, hang_up)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    hang_up.set()
    await task

    assert cancelled.is_set()


@pytest.mark.usefixtures("registry")
async def test_non_stream_completes_normally_without_disconnect() -> None:
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
    use_transport(lambda req: httpx.Response(200, json=body))
    sent = await call_app(
        {"model": "local", "messages": [{"role": "user", "content": "x"}]}, asyncio.Event()
    )
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200


@pytest.mark.usefixtures("registry")
async def test_stream_disconnect_before_first_token_cancels_upstream() -> None:
    """The longest wait (prompt processing) happens before the first chunk."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def thinking_provider(req: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    use_transport(thinking_provider)
    hang_up = asyncio.Event()
    task = asyncio.create_task(
        call_app(
            {"model": "local", "messages": [{"role": "user", "content": "x"}], "stream": True},
            hang_up,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    hang_up.set()
    sent = await task

    assert cancelled.is_set()
    assert not any(m.get("body") for m in sent)  # nothing was streamed


async def test_sse_response_closes_upstream_when_client_is_slow() -> None:
    """Disconnect while blocked writing to a slow client.

    The relay generator is parked at `yield` and the upstream at its own `yield`;
    Starlette's cancel hits `send()`, never them, and Starlette doesn't close the
    body iterator — only SSEResponse's cleanup does.
    """
    closed, writing = asyncio.Event(), asyncio.Event()

    async def upstream() -> AsyncIterator[dict[str, Any]]:
        try:
            for n in range(1000):
                yield {"n": n}
        finally:
            closed.set()

    up = upstream()
    first = await anext(up)
    resp = main.SSEResponse(main.relay_sse(first, up), upstream=up, headers={})  # type: ignore[arg-type]

    async def receive() -> dict[str, Any]:
        await writing.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        if msg["type"] == "http.response.body":
            writing.set()
            await asyncio.Event().wait()  # client stopped reading

    await asyncio.wait_for(
        resp({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 2
    )
    assert closed.is_set()


async def test_outside_cancellation_is_not_swallowed() -> None:
    from starlette.requests import Request

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()  # client never disconnects
        return {}

    request = Request({"type": "http"}, receive)
    outer = asyncio.create_task(main.cancel_on_disconnect(request, asyncio.sleep(10)))
    await asyncio.sleep(0.05)
    outer.cancel()  # e.g. server shutdown
    with pytest.raises(asyncio.CancelledError):
        await outer


async def test_disconnect_raises_client_disconnected() -> None:
    from starlette.requests import Request

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    with pytest.raises(main.ClientDisconnected):
        await main.cancel_on_disconnect(Request({"type": "http"}, receive), asyncio.sleep(10))
