"""Token-aware rate limiting and monthly budgets per API key (ADR 0007).

Each key has two token buckets — requests/min and tokens/min — that hold up to one
minute's allowance and refill continuously. A request needs room in *both*; one Lua
script checks and charges both atomically, so concurrent requests on any number of
gateway instances can't overdraw them. After the call, the token bucket is corrected
with the real usage (`adjust`).

Redis fails open (allow, log once, skip Redis for a few seconds), like the breaker.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    retry_after: float  # seconds until the request would fit (0 if allowed)
    limit_requests: int
    limit_tokens: int
    remaining_requests: float
    remaining_tokens: float

    def headers(self) -> dict[str, str]:
        """OpenAI's x-ratelimit-* headers (SDKs and dashboards already read them)."""

        def reset(remaining: float, limit: int) -> str:
            missing = max(0.0, limit - remaining)
            return _duration(missing / (limit / 60))

        out = {
            "x-ratelimit-limit-requests": str(self.limit_requests),
            "x-ratelimit-limit-tokens": str(self.limit_tokens),
            "x-ratelimit-remaining-requests": str(max(0, math.floor(self.remaining_requests))),
            "x-ratelimit-remaining-tokens": str(max(0, math.floor(self.remaining_tokens))),
            "x-ratelimit-reset-requests": reset(self.remaining_requests, self.limit_requests),
            "x-ratelimit-reset-tokens": reset(self.remaining_tokens, self.limit_tokens),
        }
        if not self.allowed:
            out["retry-after"] = str(max(1, math.ceil(self.retry_after)))
        return out


def _duration(seconds: float) -> str:
    """OpenAI style: '6m0s', '1.5s', '20ms'."""
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}m{secs:.0f}s" if minutes else f"{secs:.1f}s".replace(".0s", "s")


def allow_all(rpm: int, tpm: int) -> Verdict:
    return Verdict(True, 0.0, rpm, tpm, float(rpm), float(tpm))


class Limiter(Protocol):
    async def take(self, key_id: str, rpm: int, tpm: int, tokens: int) -> Verdict: ...
    async def adjust(self, key_id: str, tpm: int, delta: int) -> None: ...


class SpendTracker(Protocol):
    async def spent(self, key_id: str) -> float: ...
    async def add(self, key_id: str, usd: float) -> None: ...


def month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


# --- Redis -----------------------------------------------------------------

# A bucket is a hash {v: level, ts: last update}; level = min(cap, v + elapsed·rate).
# KEYS: requests bucket, tokens bucket · ARGV: rpm, tpm, token cost
_TAKE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local function level(key, cap, rate)
  local h = redis.call('HMGET', key, 'v', 'ts')
  if not h[1] then return cap end
  -- max(0, …): a Redis failover to a node with a slower clock mustn't drain the bucket
  return math.min(cap, tonumber(h[1]) + math.max(0, now - tonumber(h[2])) * rate)
end
-- Keep a bucket until it has refilled completely: a bucket in debt that expired early
-- would come back full, forgiving the debt.
local function save(key, level, cap, rate)
  redis.call('HSET', key, 'v', tostring(level), 'ts', tostring(now))
  redis.call('PEXPIRE', key, math.ceil(((cap - level) / rate + 60) * 1000))
end
local rcap, tcap, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local rrate, trate = rcap / 60, tcap / 60
local r, k = level(KEYS[1], rcap, rrate), level(KEYS[2], tcap, trate)
local wait = 0
if r < 1 then wait = math.max(wait, (1 - r) / rrate) end
-- A request bigger than the whole bucket can never "fit": let it through once the
-- bucket is full, and let the bucket go into debt.
local need = math.min(cost, tcap)
if k < need then wait = math.max(wait, (need - k) / trate) end
if wait > 0 then return {0, tostring(wait), tostring(r), tostring(k)} end
r, k = r - 1, k - cost
save(KEYS[1], r, rcap, rrate)
save(KEYS[2], k, tcap, trate)
return {1, '0', tostring(r), tostring(k)}
"""

# Correct the tokens bucket after the call. KEYS: tokens bucket · ARGV: tpm, delta
_ADJUST = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local cap, delta = tonumber(ARGV[1]), tonumber(ARGV[2])
local rate = cap / 60
local h = redis.call('HMGET', KEYS[1], 'v', 'ts')
local k = cap
if h[1] then k = math.min(cap, tonumber(h[1]) + math.max(0, now - tonumber(h[2])) * rate) end
k = math.min(cap, k - delta)
redis.call('HSET', KEYS[1], 'v', tostring(k), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(((cap - k) / rate + 60) * 1000))
return tostring(k)
"""


class _Guard:
    """Fail open on Redis errors and skip Redis for a while. Logs when an outage starts
    and ends — not on every retry. A script error (a bug, not an outage) is logged as
    an error every time."""

    BACKOFF_SECONDS = 5.0

    def __init__(self, what: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.what, self._now, self._down_until, self._outage = what, clock, 0.0, False

    @property
    def up(self) -> bool:
        return self._now() >= self._down_until

    def broken(self, exc: RedisError) -> None:
        if isinstance(exc, ResponseError):
            log.error("%s: Redis rejected a command (script bug?): %s", self.what, exc)
        elif not self._outage:
            log.warning("%s: Redis unavailable; failing open", self.what)
        self._outage = True
        self._down_until = self._now() + self.BACKOFF_SECONDS

    def ok(self) -> None:
        if self._outage:
            log.warning("%s: Redis back", self.what)
            self._outage = False


def _keys(key_id: str) -> tuple[str, str]:
    return f"rl:{{{key_id}}}:req", f"rl:{{{key_id}}}:tok"  # one cluster slot per key


class RedisLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._take = redis.register_script(_TAKE)
        self._adjust = redis.register_script(_ADJUST)
        self._guard = _Guard("rate limiter")

    async def take(self, key_id: str, rpm: int, tpm: int, tokens: int) -> Verdict:
        if not self._guard.up:
            return allow_all(rpm, tpm)
        try:
            ok, wait, r, k = await self._take(keys=list(_keys(key_id)), args=[rpm, tpm, tokens])
        except RedisError as exc:
            self._guard.broken(exc)
            return allow_all(rpm, tpm)
        self._guard.ok()
        return Verdict(bool(int(ok)), float(wait), rpm, tpm, float(r), float(k))

    async def adjust(self, key_id: str, tpm: int, delta: int) -> None:
        if delta == 0 or not self._guard.up:
            return
        try:
            await self._adjust(keys=[_keys(key_id)[1]], args=[tpm, delta])
        except RedisError as exc:
            self._guard.broken(exc)


class RedisSpend:
    TTL = 62 * 24 * 3600  # a month-to-date counter outlives its month, then goes

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._guard = _Guard("budget tracker")

    @staticmethod
    def _key(key_id: str) -> str:
        return f"spend:{{{key_id}}}:{month()}"

    async def spent(self, key_id: str) -> float:
        if not self._guard.up:
            return 0.0
        try:
            value = await self.redis.get(self._key(key_id))
        except RedisError as exc:
            self._guard.broken(exc)
            return 0.0
        self._guard.ok()
        return float(value) if value else 0.0

    async def add(self, key_id: str, usd: float) -> None:
        if usd == 0 or not self._guard.up:  # negative = a reconciliation refund
            return
        key = self._key(key_id)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incrbyfloat(key, usd)
                pipe.expire(key, self.TTL)
                await pipe.execute()
        except RedisError as exc:
            self._guard.broken(exc)


# --- memory ----------------------------------------------------------------


class MemoryLimiter:
    """Same arithmetic as the Lua scripts, for one process. Injectable clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._now = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # name → (level, ts)

    def _level(self, name: str, cap: int, now: float) -> float:
        if name not in self._buckets:
            return float(cap)
        v, ts = self._buckets[name]
        return min(cap, v + max(0.0, now - ts) * cap / 60)

    async def take(self, key_id: str, rpm: int, tpm: int, tokens: int) -> Verdict:
        now = self._now()
        rq, tk = _keys(key_id)
        r, k = self._level(rq, rpm, now), self._level(tk, tpm, now)
        wait = 0.0
        if r < 1:
            wait = max(wait, (1 - r) / (rpm / 60))
        need = min(tokens, tpm)
        if k < need:
            wait = max(wait, (need - k) / (tpm / 60))
        if wait > 0:
            return Verdict(False, wait, rpm, tpm, r, k)
        self._buckets[rq] = (r - 1, now)
        self._buckets[tk] = (k - tokens, now)
        return Verdict(True, 0.0, rpm, tpm, r - 1, k - tokens)

    async def adjust(self, key_id: str, tpm: int, delta: int) -> None:
        now = self._now()
        tk = _keys(key_id)[1]
        self._buckets[tk] = (min(tpm, self._level(tk, tpm, now) - delta), now)


class MemorySpend:
    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], float] = {}

    async def spent(self, key_id: str) -> float:
        return self._spent.get((key_id, month()), 0.0)

    async def add(self, key_id: str, usd: float) -> None:
        if usd != 0:
            k = (key_id, month())
            self._spent[k] = self._spent.get(k, 0.0) + usd


# --- estimation ------------------------------------------------------------


def estimate_prompt_tokens(messages: list[Any], chars_per_token: float) -> int:
    """Rough prompt size from characters (ADR 0007). Images count a flat 1000."""
    chars, images = 0, 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    chars += len(part.get("text", ""))
                elif part.get("type") == "image_url":
                    images += 1
    return math.ceil(chars / chars_per_token) + 1000 * images + 4 * len(messages)
