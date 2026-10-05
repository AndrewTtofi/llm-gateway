"""Circuit-breaker semantics — the same suite runs against the memory and Redis stores."""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.config import BreakerConfig
from app.routing.breaker import (
    BreakerStore,
    Decision,
    MemoryBreakerStore,
    RedisBreakerStore,
    State,
    Ticket,
)

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
CFG = BreakerConfig(failure_threshold=3, window_seconds=60, open_seconds=1, probe_timeout_seconds=5)
NORMAL = Ticket(Decision.ALLOW)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def redis_or_skip() -> Redis:
    client = Redis.from_url(REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        if os.environ.get("REQUIRE_REDIS"):
            raise
        pytest.skip("no Redis at TEST_REDIS_URL (make up); CI sets REQUIRE_REDIS")
    return client


@pytest.fixture(params=["memory", "redis"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[tuple[BreakerStore, Clock | None]]:
    if request.param == "memory":
        clock = Clock()
        yield MemoryBreakerStore(clock=clock), clock
        return
    client = await redis_or_skip()
    yield RedisBreakerStore(client), None
    await client.aclose()


async def wait(clock: Clock | None, seconds: float) -> None:
    if clock is None:
        await asyncio.sleep(seconds)  # Redis expiry is real time
    else:
        clock.t += seconds


def target() -> str:
    return f"test/{uuid.uuid4().hex[:8]}"  # unique per test: Redis state is shared


async def trip(s: BreakerStore, t: str) -> None:
    for _ in range(CFG.failure_threshold):
        await s.record_failure(t, CFG, NORMAL)


async def half_open(s: BreakerStore, clock: Clock | None, t: str) -> Ticket:
    await trip(s, t)
    await wait(clock, CFG.open_seconds + 0.2)
    ticket = await s.decide(t, CFG)
    assert ticket.decision is Decision.PROBE and ticket.token
    return ticket


async def test_closed_until_threshold(store: tuple[BreakerStore, Clock | None]) -> None:
    s, _ = store
    t = target()
    for _ in range(CFG.failure_threshold - 1):
        assert await s.record_failure(t, CFG, NORMAL) is False
        assert (await s.decide(t, CFG)).decision is Decision.ALLOW
    assert await s.record_failure(t, CFG, NORMAL) is True  # this one opens it
    assert (await s.decide(t, CFG)).decision is Decision.DENY
    assert await s.state(t) is State.OPEN


async def test_half_open_allows_exactly_one_probe(store: tuple[BreakerStore, Clock | None]) -> None:
    s, clock = store
    t = target()
    await half_open(s, clock, t)
    assert await s.state(t) is State.HALF_OPEN
    assert (await s.decide(t, CFG)).decision is Decision.DENY  # one probe at a time


async def test_probe_success_closes(store: tuple[BreakerStore, Clock | None]) -> None:
    s, clock = store
    t = target()
    probe = await half_open(s, clock, t)
    await s.record_success(t, CFG, probe)
    assert await s.state(t) is State.CLOSED
    assert (await s.decide(t, CFG)).decision is Decision.ALLOW
    assert await s.record_failure(t, CFG, NORMAL) is False  # a fresh threshold to open again


async def test_probe_failure_reopens(store: tuple[BreakerStore, Clock | None]) -> None:
    s, clock = store
    t = target()
    probe = await half_open(s, clock, t)
    assert await s.record_failure(t, CFG, probe) is True  # one failure when half-open
    assert (await s.decide(t, CFG)).decision is Decision.DENY


async def test_straggler_success_does_not_close_an_open_breaker(
    store: tuple[BreakerStore, Clock | None],
) -> None:
    """A request that started before the trip and succeeds after it changes nothing."""
    s, _ = store
    t = target()
    await trip(s, t)
    await s.record_success(t, CFG, NORMAL)
    assert await s.state(t) is State.OPEN


async def test_straggler_failures_dont_extend_or_reset(
    store: tuple[BreakerStore, Clock | None],
) -> None:
    s, clock = store
    t = target()
    probe = await half_open(s, clock, t)
    assert await s.record_failure(t, CFG, NORMAL) is False  # not the probe: ignored
    assert await s.state(t) is State.HALF_OPEN
    await s.record_success(t, CFG, probe)
    assert await s.state(t) is State.CLOSED


async def test_released_probe_frees_the_slot(store: tuple[BreakerStore, Clock | None]) -> None:
    """A probe with no verdict (client hung up) mustn't block the target until timeout."""
    s, clock = store
    t = target()
    probe = await half_open(s, clock, t)
    await s.release(t, probe)
    assert (await s.decide(t, CFG)).decision is Decision.PROBE


async def test_a_stale_token_cannot_settle_a_newer_probe(
    store: tuple[BreakerStore, Clock | None],
) -> None:
    s, clock = store
    t = target()
    old = await half_open(s, clock, t)
    await s.release(t, old)
    new = await s.decide(t, CFG)
    await s.record_success(t, CFG, old)  # late answer from the abandoned probe
    assert await s.state(t) is State.HALF_OPEN
    await s.record_success(t, CFG, new)
    assert await s.state(t) is State.CLOSED


async def test_failures_outside_the_window_dont_add_up() -> None:
    clock = Clock()
    s = MemoryBreakerStore(clock=clock)
    t = target()
    for _ in range(CFG.failure_threshold - 1):
        await s.record_failure(t, CFG, NORMAL)
    clock.t += CFG.window_seconds + 1
    assert await s.record_failure(t, CFG, NORMAL) is False
    assert await s.state(t) is State.CLOSED


async def test_tripped_state_expires_if_nobody_probes() -> None:
    clock = Clock()
    s = MemoryBreakerStore(clock=clock)
    t = target()
    await trip(s, t)
    clock.t += CFG.open_seconds + CFG.probe_timeout_seconds + CFG.window_seconds + 1
    assert await s.state(t) is State.CLOSED


async def test_memory_store_prunes_expired_targets() -> None:
    clock = Clock()
    s = MemoryBreakerStore(clock=clock)
    for _ in range(s.MAX_TRACKED + 10):
        await s.record_failure(target(), CFG, NORMAL)
    clock.t += CFG.window_seconds + 1
    await s.record_failure(target(), CFG, NORMAL)
    assert len(s._fails) == 1


async def test_redis_keys_share_one_cluster_slot() -> None:
    client = await redis_or_skip()
    s = RedisBreakerStore(client)
    t = target()
    await trip(s, t)
    keys = sorted([k.decode() async for k in client.scan_iter(f"cb:{{{t}}}:*")])
    assert keys == [f"cb:{{{t}}}:open", f"cb:{{{t}}}:tripped"]
    assert await client.ttl(f"cb:{{{t}}}:tripped") > 0  # no immortal keys
    await client.aclose()


async def test_redis_down_fails_open_fast_and_stops_asking() -> None:
    dead = Redis.from_url(
        "redis://10.255.255.1:6379/0", socket_connect_timeout=0.1, socket_timeout=0.1
    )
    s = RedisBreakerStore(dead)
    t = target()
    assert (await s.decide(t, CFG)).decision is Decision.ALLOW  # first call pays the timeout
    start = time.perf_counter()
    for _ in range(20):  # the rest skip Redis entirely during the backoff
        assert (await s.decide(t, CFG)).decision is Decision.ALLOW
        assert await s.record_failure(t, CFG, NORMAL) is False
    assert time.perf_counter() - start < 0.05
    await dead.aclose()
