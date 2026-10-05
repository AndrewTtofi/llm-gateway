"""Token buckets and spend — the same suite against the memory and Redis stores."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.ratelimit import (
    Limiter,
    MemoryLimiter,
    MemorySpend,
    RedisLimiter,
    RedisSpend,
    SpendTracker,
    Verdict,
    estimate_prompt_tokens,
)
from tests.test_breaker import Clock, redis_or_skip


@pytest.fixture(params=["memory", "redis"])
async def limiter(request: pytest.FixtureRequest) -> AsyncIterator[tuple[Limiter, Clock | None]]:
    if request.param == "memory":
        clock = Clock()
        yield MemoryLimiter(clock=clock), clock
        return
    client = await redis_or_skip()
    yield RedisLimiter(client), None
    await client.aclose()


def kid() -> str:
    return f"k-{uuid.uuid4().hex[:8]}"


async def advance(clock: Clock | None, seconds: float) -> None:
    if clock is None:
        await asyncio.sleep(seconds)
    else:
        clock.t += seconds


async def test_burst_up_to_the_per_minute_allowance(limiter: tuple[Limiter, Clock | None]) -> None:
    lim, _ = limiter
    k = kid()
    results = [await lim.take(k, rpm=10, tpm=10**6, tokens=1) for _ in range(12)]
    assert [r.allowed for r in results] == [True] * 10 + [False] * 2
    denied = results[-1]
    assert 0 < denied.retry_after <= 6.1  # one request refills every 6s at 10/min


async def test_refills_continuously(limiter: tuple[Limiter, Clock | None]) -> None:
    lim, clock = limiter
    k = kid()
    for _ in range(60):
        await lim.take(k, rpm=60, tpm=10**6, tokens=1)  # drain: refills 1/s
    assert not (await lim.take(k, rpm=60, tpm=10**6, tokens=1)).allowed
    await advance(clock, 1.1)
    assert (await lim.take(k, rpm=60, tpm=10**6, tokens=1)).allowed


async def test_token_bucket_limits_independently(limiter: tuple[Limiter, Clock | None]) -> None:
    lim, _ = limiter
    k = kid()
    assert (await lim.take(k, rpm=100, tpm=1000, tokens=700)).allowed
    denied = await lim.take(k, rpm=100, tpm=1000, tokens=700)  # requests fine, tokens not
    assert not denied.allowed
    assert denied.remaining_requests >= 98  # a denied request charges nothing
    assert 20 < denied.retry_after < 25  # 400 missing tokens at 1000/60 per second


async def test_request_bigger_than_the_bucket_still_goes_through_once(
    limiter: tuple[Limiter, Clock | None],
) -> None:
    lim, _ = limiter
    k = kid()
    big = await lim.take(k, rpm=100, tpm=1000, tokens=5000)  # can never "fit"
    assert big.allowed and big.remaining_tokens < 0  # allowed from a full bucket, into debt
    assert not (await lim.take(k, rpm=100, tpm=1000, tokens=1)).allowed  # debt delays others


async def test_adjust_refunds_and_charges(limiter: tuple[Limiter, Clock | None]) -> None:
    lim, _ = limiter
    k = kid()
    await lim.take(k, rpm=100, tpm=1000, tokens=900)
    await lim.adjust(k, tpm=1000, delta=-800)  # estimate was 900, actual 100
    v = await lim.take(k, rpm=100, tpm=1000, tokens=1)
    assert v.allowed and 890 < v.remaining_tokens < 900
    await lim.adjust(k, tpm=1000, delta=+2000)  # actual far above the estimate → debt
    assert not (await lim.take(k, rpm=100, tpm=1000, tokens=1)).allowed


async def test_limits_enforced_within_5_percent() -> None:
    """Phase 4 DoD: a client hammering for 10 s gets burst + refill, ±5%."""
    clock = Clock()
    lim = MemoryLimiter(clock=clock)
    k, rpm, seconds = kid(), 60, 10.0
    allowed = 0
    for _ in range(int(seconds * 100)):  # 100 attempts/s, far above the limit
        allowed += (await lim.take(k, rpm=rpm, tpm=10**9, tokens=1)).allowed
        clock.t += 0.01
    expected = rpm + rpm / 60 * seconds  # full bucket + refill
    assert abs(allowed - expected) / expected <= 0.05, (allowed, expected)


async def test_limits_enforced_within_5_percent_on_redis_across_instances() -> None:
    """Same, real Redis, two "gateway instances" sharing one bucket with 50 concurrent
    clients — far more attempts than the limit, so the bucket is what decides."""
    client = await redis_or_skip()
    instances = [RedisLimiter(client), RedisLimiter(client)]
    k, rpm, seconds = kid(), 120, 3.0
    allowed = attempts = 0
    deadline = time.monotonic() + seconds

    async def hammer(lim: RedisLimiter) -> None:
        nonlocal allowed, attempts
        while time.monotonic() < deadline:
            verdict = await lim.take(k, rpm=rpm, tpm=10**9, tokens=1)
            # Increment *after* the await: `allowed += await …` reads `allowed` before
            # suspending, so concurrent coroutines would overwrite each other's counts.
            attempts += 1
            allowed += verdict.allowed

    await asyncio.gather(*(hammer(instances[i % 2]) for i in range(50)))
    expected = rpm + rpm / 60 * seconds
    assert attempts > 3 * expected  # saturated: the limiter, not the client, set the rate
    assert abs(allowed - expected) / expected <= 0.05, (allowed, expected, attempts)
    await client.aclose()


def test_ratelimit_headers_openai_style() -> None:
    v = Verdict(False, 2.3, 60, 1000, 0.4, 500)
    h = v.headers()
    assert h["x-ratelimit-limit-requests"] == "60"
    assert h["x-ratelimit-remaining-requests"] == "0"
    assert h["x-ratelimit-remaining-tokens"] == "500"
    assert h["x-ratelimit-reset-tokens"] == "30s"  # 500 missing at 1000/min
    assert h["retry-after"] == "3"  # whole seconds, rounded up
    assert "retry-after" not in Verdict(True, 0, 60, 1000, 59, 1000).headers()


async def test_redis_down_fails_open() -> None:
    dead = Redis.from_url(
        "redis://10.255.255.1:6379/0", socket_connect_timeout=0.1, socket_timeout=0.1
    )
    lim, spend = RedisLimiter(dead), RedisSpend(dead)
    k = kid()
    for _ in range(5):
        assert (await lim.take(k, rpm=1, tpm=1, tokens=10)).allowed
    assert await spend.spent(k) == 0.0
    await dead.aclose()


@pytest.fixture(params=["memory", "redis"])
async def spend(request: pytest.FixtureRequest) -> AsyncIterator[SpendTracker]:
    if request.param == "memory":
        yield MemorySpend()
        return
    client = await redis_or_skip()
    yield RedisSpend(client)
    await client.aclose()


async def test_spend_accumulates_per_key(spend: SpendTracker) -> None:
    a, b = kid(), kid()
    await spend.add(a, 0.25)
    await spend.add(a, 0.5)
    await spend.add(b, 1.0)
    await spend.add(a, 0)  # no-op
    assert await spend.spent(a) == pytest.approx(0.75)
    assert await spend.spent(b) == pytest.approx(1.0)


def test_prompt_estimate() -> None:
    msgs = [
        {"role": "user", "content": "x" * 400},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "y" * 40},
                {"type": "image_url", "image_url": {"url": "u"}},
            ],
        },
    ]
    assert estimate_prompt_tokens(msgs, 4) == 100 + 10 + 1000 + 8


async def test_redis_bucket_lives_until_its_debt_is_repaid() -> None:
    """Review: a 120s TTL forgave any debt deeper than one bucket."""
    client = await redis_or_skip()
    lim = RedisLimiter(client)
    k = kid()
    v = await lim.take(k, rpm=100, tpm=1000, tokens=10_000)  # -9000: 600s to refill
    assert v.allowed and v.remaining_tokens < -8000
    ttl_ms = await client.pttl(f"rl:{{{k}}}:tok")
    assert ttl_ms > 590_000  # not 120s
    denied = await lim.take(k, rpm=100, tpm=1000, tokens=1)
    assert not denied.allowed and denied.retry_after > 500  # and it says so honestly
    await client.aclose()
