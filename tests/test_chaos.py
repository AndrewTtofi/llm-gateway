"""Phase 3 DoD: with the primary forced to fail, 100% of requests succeed via fallback,
and the breaker opens and recovers."""

import asyncio

import httpx
import pytest

from app import main
from app.config import Registry
from app.routing import router
from app.routing.breaker import MemoryBreakerStore, State

N = 100


@pytest.mark.usefixtures("registry")
async def test_primary_down_every_request_still_succeeds_and_breaker_recovers(
    monkeypatch: pytest.MonkeyPatch,
    auth: dict[str, str],
) -> None:
    now = [0.0]
    store = MemoryBreakerStore(clock=lambda: now[0])
    monkeypatch.setattr(router, "store", store)
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://gw", headers=auth) as gw:

        async def one(stream: bool) -> httpx.Response:
            body = {
                "model": "down-then-ok",
                "messages": [{"role": "user", "content": "x"}],
                "stream": stream,
            }
            return await gw.post("/v1/chat/completions", json=body)

        # 100 concurrent requests, half streaming, while the primary is 100% down
        responses = await asyncio.gather(*(one(i % 2 == 0) for i in range(N)))

    assert [r.status_code for r in responses].count(200) == N  # 100% success
    assert all(r.headers["x-gateway-provider"] == "chaos/ok" for r in responses)
    assert await store.state("chaos/down") is State.OPEN
    # Once open, requests stop paying for the dead primary: most went straight through.
    skipped = sum(1 for r in responses if r.headers["x-gateway-attempts"] == "1")
    assert skipped > N / 2

    # Recovery: the primary comes back, open_seconds pass, the probe succeeds.
    reg: Registry = main.config.registry
    reg.providers["chaos"]["models"]["down"] = {}
    now[0] += reg.circuit_breaker.open_seconds + 1
    assert await store.state("chaos/down") is State.HALF_OPEN
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", headers=auth) as gw:
        r = await gw.post(
            "/v1/chat/completions",
            json={"model": "down-then-ok", "messages": [{"role": "user", "content": "x"}]},
        )
    assert r.headers["x-gateway-provider"] == "chaos/down"
    assert await store.state("chaos/down") is State.CLOSED
