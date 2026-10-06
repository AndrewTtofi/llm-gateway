"""Token-aware rate limiting and monthly budgets per API key (ADR 0007).

Each key has two token buckets — requests/min and tokens/min — that hold up to one
minute's allowance and refill continuously. A request needs room in *both*; one Lua
script checks and charges both atomically, so concurrent requests on any number of
gateway instances can't overdraw them. After the call, the token bucket is corrected
with the real usage (`adjust`).

Redis fails open (allow, log once, skip Redis for a few seconds), like the breaker.
Spend added while Redis is unreachable is kept and written when it's back, and budget
checks meanwhile use the last known spend (ADR 0023).
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

from app.observability import metrics

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
    """`period` (YYYY-MM): which month's counter. A request reserves and settles in the
    month it started, so a refund at midnight on the 1st doesn't land in the new month."""

    async def spent(self, key_id: str) -> float: ...
    async def add(self, key_id: str, usd: float, period: str | None = None) -> None: ...
    async def reserve(
        self, key_id: str, usd: float, budget: float, period: str | None = None
    ) -> bool:
        """Add `usd` only if spend is still under `budget`, in one atomic step, so
        concurrent requests can't all pass the same stale check (ADR 0023)."""
        ...


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
        if self._now() >= self._down_until:
            return True
        metrics.fail_open.labels(self.what).inc()
        return False

    def broken(self, exc: RedisError) -> None:
        metrics.fail_open.labels(self.what).inc()
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


# Check and add in one step. KEYS: spend counter · ARGV: usd, budget, ttl
_RESERVE = """
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
if spent >= tonumber(ARGV[2]) then return 0 end
redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


class RedisSpend:
    TTL = 62 * 24 * 3600  # a month-to-date counter outlives its month, then goes
    MAX_TRACKED = 10_000  # keys whose pending or last known spend this replica remembers

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._guard = _Guard("budget tracker")
        # Spend not yet in Redis, by Redis key (so it lands in the month it was spent).
        self._pending: dict[str, float] = {}
        self._known: dict[str, float] = {}  # last spend read per Redis key
        self._dropping = False  # logged that the queue is full
        self._reserve = redis.register_script(_RESERVE)

    @staticmethod
    def _key(key_id: str, period: str | None = None) -> str:
        return f"spend:{{{key_id}}}:{period or month()}"

    def _remember(self, key: str, value: float) -> None:
        self._known.pop(key, None)
        self._known[key] = value
        if len(self._known) > self.MAX_TRACKED:
            self._known.pop(next(iter(self._known)))

    def _local(self, key: str) -> float:
        """Best guess without Redis: last known spend plus what's queued here."""
        return self._known.get(key, 0.0) + self._pending.get(key, 0.0)

    def _defer(self, key: str, usd: float) -> None:
        if key in self._pending or len(self._pending) < self.MAX_TRACKED:
            self._pending[key] = self._pending.get(key, 0.0) + usd
        elif not self._dropping:
            self._dropping = True
            log.error("budget tracker: too much spend queued while Redis is down; dropping")

    async def _flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                for key, usd in pending.items():
                    pipe.incrbyfloat(key, usd)
                    pipe.expire(key, self.TTL)
                await pipe.execute()
        except BaseException as exc:
            # Not written (or not known to be): keep it. If EXEC did apply and only the
            # reply was lost, it's counted twice; over-counting is the safe side.
            for key, usd in pending.items():
                self._defer(key, usd)
            if not isinstance(exc, RedisError):
                raise  # e.g. cancelled: the queue survives for the next flush
            self._guard.broken(exc)

    async def spent(self, key_id: str) -> float:
        key = self._key(key_id)
        if not self._guard.up:
            return self._local(key)
        await self._flush()  # first, so the read includes it
        try:
            value = await self.redis.get(key)
        except RedisError as exc:
            self._guard.broken(exc)
            return self._local(key)
        self._guard.ok()
        spent = float(value) if value else 0.0
        self._remember(key, spent)
        return spent + self._pending.get(key, 0.0)

    async def reserve(
        self, key_id: str, usd: float, budget: float, period: str | None = None
    ) -> bool:
        key = self._key(key_id, period)
        if not self._guard.up:  # fail open, but not past what this replica knows
            if self._local(key) >= budget:
                return False
            self._defer(key, usd)
            return True
        await self._flush()
        try:
            ok = bool(int(await self._reserve(keys=[key], args=[usd, budget, self.TTL])))
        except RedisError as exc:
            self._guard.broken(exc)
            if self._local(key) >= budget:
                return False
            self._defer(key, usd)
            return True
        self._guard.ok()
        if ok and key in self._known:
            self._known[key] += usd
        return ok

    async def add(self, key_id: str, usd: float, period: str | None = None) -> None:
        if usd == 0:  # negative = a reconciliation refund
            return
        key = self._key(key_id, period)
        if not self._guard.up:
            self._defer(key, usd)
            return
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incrbyfloat(key, usd)
                pipe.expire(key, self.TTL)
                await pipe.execute()
        except RedisError as exc:
            self._guard.broken(exc)
            self._defer(key, usd)
            return
        if key in self._known:
            self._known[key] += usd  # so an outage starts from an up-to-date figure
        await self._flush()


# --- concurrency -----------------------------------------------------------


class Concurrency:
    """Requests in flight per key, on this replica (ADR 0023). Per replica on purpose: what
    it protects, the provider connection pools, is per replica too. Without it, one key
    could hold every connection (long streams read slowly) and starve other tenants."""

    def __init__(self) -> None:
        self._active: dict[str, int] = {}

    def acquire(self, key_id: str, limit: int) -> Callable[[], None] | None:
        """A release function, or None when the key is at its limit (0 = no limit)."""
        n = self._active.get(key_id, 0)
        if limit and n >= limit:
            return None
        self._active[key_id] = n + 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            left = self._active.get(key_id, 1) - 1
            if left > 0:
                self._active[key_id] = left
            else:
                self._active.pop(key_id, None)

        return release

    def active(self, key_id: str) -> int:
        return self._active.get(key_id, 0)


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

    async def add(self, key_id: str, usd: float, period: str | None = None) -> None:
        if usd != 0:
            k = (key_id, period or month())
            self._spent[k] = self._spent.get(k, 0.0) + usd

    async def reserve(
        self, key_id: str, usd: float, budget: float, period: str | None = None
    ) -> bool:
        k = (key_id, period or month())  # no await between the check and the add: atomic
        if self._spent.get(k, 0.0) >= budget:
            return False
        self._spent[k] = self._spent.get(k, 0.0) + usd
        return True


# --- estimation ------------------------------------------------------------


# Base64 file and audio payloads count a tenth of their characters: a PDF's base64 is
# ~10x more characters than the tokens a provider bills for it. Still generous, and it
# keeps the estimate (billed as-is when a request is cut off) near the real size.
BASE64_SHARE = 0.1
_PAYLOAD_KEYS = frozenset({"file_data", "data"})


def _chars(value: Any, depth: int = 0, payload: bool = False) -> int:
    """Characters in every string inside `value` (keys included): tool schemas, file and
    audio payloads, tool-call arguments. Depth-bounded; parsed JSON is shallow anyway."""
    if isinstance(value, str):
        if payload or value.startswith("data:"):
            return math.ceil(len(value) * BASE64_SHARE)
        return len(value)
    if depth > 32:
        return 0
    if isinstance(value, dict):
        return sum(len(str(k)) + _chars(v, depth + 1, k in _PAYLOAD_KEYS) for k, v in value.items())
    if isinstance(value, list):
        return sum(_chars(v, depth + 1) for v in value)
    return 0


def estimate_prompt_tokens(messages: list[Any], chars_per_token: float, tools: Any = None) -> int:
    """Rough prompt size from characters (ADR 0007). Counts everything the provider reads:
    text, tool definitions, tool calls, thinking blocks and file/audio payloads (base64
    at a tenth of its characters). Images count a flat 1000 each."""
    chars, images = _chars(tools) if tools else 0, 0
    for m in messages:
        if not isinstance(m, dict):
            m = m.model_dump() if hasattr(m, "model_dump") else {}
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    chars += len(part.get("text") or "")
                elif part.get("type") == "image_url":
                    images += 1
                else:
                    chars += _chars(part)
        chars += _chars(m.get("tool_calls")) + _chars(m.get("thinking_blocks"))
    return math.ceil(chars / chars_per_token) + 1000 * images + 4 * len(messages)
