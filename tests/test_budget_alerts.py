"""Budget alerts at 50 / 80 / 100% of a monthly budget (ADR 0024)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import budget_alerts, config, services
from app.auth import ApiKey, EffectiveLimits
from app.config import Limits, Pricing
from app.metering import Meter
from app.ratelimit import MemoryLimiter, MemorySpend, RedisSpend
from app.routing.selfheal import Alerts


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def deliver(self, payload: dict[str, Any], dedupe: str, ttl: float) -> None:
        self.sent.append({**payload, "dedupe": dedupe})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(services, "alerts", r)
    # $1 per estimated request: 1M input tokens at $1/M, no output
    monkeypatch.setattr(
        config, "pricing", Pricing.model_validate({"models": {"p/m": {"input": 1, "output": 0}}})
    )
    return r


def meter(spend: MemorySpend, budget: float, team: str | None = None) -> Meter:
    key = ApiKey(id="k1", name="billing-app", prefix="gw_abc", tier="dev", team=team)
    lim = EffectiveLimits(60, 10**9, budget, ("*",))
    return Meter(key, lim, MemoryLimiter(), spend, 1_000_000, 1_000_000, False)


async def drain() -> None:
    await asyncio.gather(*list(budget_alerts._tasks))


async def test_each_level_alerts_once(recorder: Recorder) -> None:
    spend = MemorySpend()
    for _ in range(10):  # ten $1 holds against a $10 budget
        await meter(spend, 10).reserve("p/m")
    await drain()
    levels = [a["level"] for a in recorder.sent]
    assert levels == [50, 80, 100]  # each crossed once, in order
    assert "key gw_abc (billing-app)" in recorder.sent[0]["text"]
    assert recorder.sent[-1]["dedupe"].startswith("budget-alert:{k1}:")


async def test_teams_get_their_own_alerts(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    spend = MemorySpend()
    await spend.add("team:web", 3.5)
    await meter(spend, 100, team="web").reserve("p/m", team_budget=5)  # team 3.5 → 4.5
    await drain()
    assert [(a["who"], a["level"]) for a in recorder.sent] == [("team web", 80)]


async def test_a_jump_past_several_levels_sends_each(recorder: Recorder) -> None:
    spend = MemorySpend()
    await spend.add("k1", 0.4)
    await meter(spend, 1).reserve("p/m")  # $0.40 → $1.40: past 50, 80 and 100%
    await drain()
    assert sorted(a["level"] for a in recorder.sent) == [50, 80, 100]


def test_levels_must_be_shares() -> None:
    with pytest.raises(ValueError, match="shares"):
        Limits.model_validate({"tiers": {}, "budget_alerts": [0.5, 1.5]})
    assert Limits.model_validate({"tiers": {}, "budget_alerts": [1, 0.5, 0.5]}).budget_alerts == [
        0.5,
        1,
    ]


async def test_one_replica_sends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []

    async def post(url: str, payload: dict[str, Any]) -> None:
        posted.append(payload)

    class Locks:
        def __init__(self) -> None:
            self.taken: set[str] = set()

        async def set(self, key: str, value: str, nx: bool, ex: int) -> bool:
            if key in self.taken:
                return False
            self.taken.add(key)
            return True

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    locks = Locks()
    replicas = [Alerts(redis=locks, post=post), Alerts(redis=locks, post=post)]
    for a in replicas:
        await a.deliver({"text": "80%"}, dedupe="budget-alert:{k1}:2026-10:80", ttl=60)
    assert len(posted) == 1


async def test_redis_reservation_returns_the_new_total() -> None:
    from tests.test_breaker import redis_or_skip

    redis = await redis_or_skip()
    try:
        spend = RedisSpend(redis)
        key = "alert-test-key"
        await redis.delete(spend._key(key, "2026-01"))
        assert await spend.reserve(key, 3.0, 10, "2026-01") == pytest.approx(3.0)
        assert await spend.reserve(key, 4.5, 10, "2026-01") == pytest.approx(7.5)
        await spend.add(key, 5, "2026-01")
        assert await spend.reserve(key, 1.0, 10, "2026-01") is None  # at 12.5: refused
        await redis.delete(spend._key(key, "2026-01"))
    finally:
        await redis.aclose()


def test_small_amounts_keep_their_digits() -> None:
    assert budget_alerts.usd(0.0012) == "$0.0012"
    assert budget_alerts.usd(1234.5) == "$1,234.50"
