"""Self-healing: quarantine, background probes, alerts (ADR 0019)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import BreakerConfig, Registry
from app.routing import router, selfheal
from app.routing.breaker import MemoryBreakerStore, State, Ticket

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture
def clock(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(router, "store", MemoryBreakerStore(clock=lambda: now[0]))
    return now


async def trip(target: str, cfg: BreakerConfig) -> None:
    for _ in range(cfg.failure_threshold):
        await router.store.record_failure(target, cfg, Ticket(router.Decision.ALLOW))


def test_auth_failure_quarantines_the_target(
    client: TestClient, registry: Registry, clock: list[float]
) -> None:
    import asyncio

    resp = client.post("/v1/chat/completions", json={"model": "auth-then-ok", "messages": MSGS})
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert asyncio.run(router.store.state("chaos/unauthorized")) == State.OPEN
    assert asyncio.run(router.store.reason("chaos/unauthorized")) == "authentication failed"
    clock[0] += registry.circuit_breaker.open_seconds + 1  # an ordinary open would be over
    assert asyncio.run(router.store.state("chaos/unauthorized")) == State.OPEN  # quarantined
    clock[0] += registry.self_healing.quarantine_seconds
    assert asyncio.run(router.store.state("chaos/unauthorized")) == State.HALF_OPEN


def test_transient_failures_are_not_quarantined(client: TestClient, clock: list[float]) -> None:
    import asyncio

    for _ in range(3):
        client.post("/v1/chat/completions", json={"model": "down-then-ok", "messages": MSGS})
    assert asyncio.run(router.store.reason("chaos/down")) is None


async def test_probe_recovers_a_healthy_target(registry: Registry, clock: list[float]) -> None:
    cfg = registry.circuit_breaker
    await trip("chaos/ok", cfg)
    assert await router.store.state("chaos/ok") == State.OPEN
    assert await selfheal.probe_once("chaos/ok") == "busy"  # still open: no probe yet
    clock[0] += cfg.open_seconds + 1
    assert await router.store.state("chaos/ok") == State.HALF_OPEN
    assert await selfheal.probe_once("chaos/ok") == "recovered"
    assert await router.store.state("chaos/ok") == State.CLOSED


async def test_probe_reopens_a_still_broken_target(registry: Registry, clock: list[float]) -> None:
    cfg = registry.circuit_breaker
    await trip("chaos/down", cfg)
    clock[0] += cfg.open_seconds + 1
    assert await selfheal.probe_once("chaos/down") == "failed"
    assert await router.store.state("chaos/down") == State.OPEN


async def test_probe_skips_unconfigured_providers(
    registry: Registry, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOCK_API_KEY")
    cfg = registry.circuit_breaker
    await trip("mock/tiny", cfg)
    clock[0] += cfg.open_seconds + 1
    assert await selfheal.probe_once("mock/tiny") == "skipped"
    assert await router.store.state("mock/tiny") == State.HALF_OPEN  # untouched


async def test_quarantine_in_redis() -> None:
    from app.routing.breaker import RedisBreakerStore
    from tests.test_breaker import redis_or_skip

    redis = await redis_or_skip()
    store = RedisBreakerStore(redis)
    cfg = BreakerConfig(open_seconds=1)
    try:
        await store.quarantine("q/test", cfg, 60, "quota exhausted")
        assert await store.state("q/test") == State.OPEN
        assert await store.reason("q/test") == "quota exhausted"
        assert await redis.ttl("cb:{q/test}:open") > 30  # longer than open_seconds
    finally:
        await redis.delete(
            *[f"cb:{{q/test}}:{k}" for k in ("fails", "open", "tripped", "probe", "why")]
        )
        await redis.aclose()


# --- alerts ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool:
        if nx and key in self.keys:
            return False
        self.keys.add(key)
        return True


@pytest.fixture
def posted(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    return []


async def test_alerts_on_state_changes_only(
    posted: list[dict[str, Any]], clock: list[float]
) -> None:
    async def post(url: str, payload: dict[str, Any]) -> None:
        posted.append(payload)

    alerts = selfheal.Alerts(post=post)
    await alerts.observe("a/b", "closed")  # first sight: no alert
    await alerts.observe("a/b", "closed")
    await alerts.observe("a/b", "open")
    await alerts.observe("a/b", "closed")
    assert [(p["from"], p["to"]) for p in posted] == [("closed", "open"), ("open", "closed")]
    assert "a/b circuit closed → open" in posted[0]["text"]


async def test_alerts_include_the_quarantine_reason(
    posted: list[dict[str, Any]], registry: Registry, clock: list[float]
) -> None:
    async def post(url: str, payload: dict[str, Any]) -> None:
        posted.append(payload)

    alerts = selfheal.Alerts(post=post)
    await alerts.observe("chaos/ok", "closed")
    await router.store.quarantine("chaos/ok", registry.circuit_breaker, 600, "quota exhausted")
    await alerts.observe("chaos/ok", "open")
    assert posted[0]["reason"] == "quota exhausted" and "(quota exhausted)" in posted[0]["text"]


async def test_one_replica_sends_each_alert(
    posted: list[dict[str, Any]], clock: list[float]
) -> None:
    async def post(url: str, payload: dict[str, Any]) -> None:
        posted.append(payload)

    shared = FakeRedis()
    replicas = [selfheal.Alerts(redis=shared, post=post) for _ in range(3)]
    for r in replicas:
        await r.observe("a/b", "closed")
        await r.observe("a/b", "open")
    assert len(posted) == 1


async def test_no_webhook_no_alerts(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    sent: list[Any] = []

    async def post(url: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    alerts = selfheal.Alerts(post=post)
    await alerts.observe("a/b", "closed")
    await alerts.observe("a/b", "open")
    assert sent == []


async def test_failing_webhook_doesnt_raise(
    posted: list[dict[str, Any]], clock: list[float]
) -> None:
    async def post(url: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("down")

    alerts = selfheal.Alerts(post=post)
    await alerts.observe("a/b", "closed")
    await alerts.observe("a/b", "open")  # logged and counted, not raised
