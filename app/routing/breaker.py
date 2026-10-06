"""Circuit breaker per routing target (`provider/model`). See ADR 0004.

closed ── failure_threshold failures within window_seconds ──▶ open
open   ── open_seconds pass ──▶ half-open: ONE probe request is allowed
half-open ── the probe succeeds ──▶ closed   ·   the probe fails ──▶ open again

Only the probe decides how a half-open breaker ends. The probe holds a unique token, so
a straggler that started before the breaker opened can't close it by succeeding, and a
probe that ends without a verdict (client hung up, request unsupported) gives its slot
back instead of blocking the target until the probe times out.

Two stores, same semantics: Redis (shared by all gateway instances, atomic Lua
scripts) and memory (one process; dev and tests). If Redis is unreachable the breaker
fails open, and stops asking Redis for a few seconds so an outage doesn't add a timeout
to every request.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import BreakerConfig

log = logging.getLogger(__name__)


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Decision(StrEnum):
    ALLOW = "allow"  # closed: normal traffic
    PROBE = "probe"  # half-open: this request is the probe
    DENY = "deny"  # open, or half-open with a probe already in flight


@dataclass(frozen=True)
class Ticket:
    decision: Decision
    token: str | None = None  # set for PROBE: proves which request is the probe


class BreakerStore(Protocol):
    async def decide(self, target: str, cfg: BreakerConfig) -> Ticket: ...
    async def record_success(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> None: ...
    async def record_failure(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> bool: ...
    async def release(self, target: str, ticket: Ticket) -> None: ...
    async def state(self, target: str) -> State: ...
    async def quarantine(
        self, target: str, cfg: BreakerConfig, seconds: float, reason: str
    ) -> None: ...
    async def reason(self, target: str) -> str | None: ...


def tripped_ttl(cfg: BreakerConfig) -> float:
    """How long a tripped breaker remembers it's tripped if nobody ever probes it."""
    return cfg.open_seconds + cfg.probe_timeout_seconds + cfg.window_seconds


# --- Redis -----------------------------------------------------------------
# Keys per target, with a {hash tag} so all four live in one Redis Cluster slot:
#   cb:{t}:fails    failure counter, expires with the window
#   cb:{t}:oks      successes since the window's first failure, same expiry
#   cb:{t}:open     exists while open, expires after open_seconds
#   cb:{t}:tripped  exists from opening until closed (open + half-open)
#   cb:{t}:probe    token of the in-flight probe, expires after probe_timeout

_DECIDE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then
  if redis.call('SET', KEYS[3], ARGV[1], 'NX', 'EX', ARGV[2]) then return 2 end
  return 0
end
return 1
"""

# KEYS: fails, open, tripped, probe, oks · ARGV: token, window, threshold, open_s,
# tripped_ttl, failure_rate
_FAIL = """
if ARGV[1] ~= '' then
  if redis.call('GET', KEYS[4]) == ARGV[1] then
    redis.call('SET', KEYS[2], '1', 'EX', ARGV[4])
    redis.call('SET', KEYS[3], '1', 'EX', ARGV[5])
    redis.call('DEL', KEYS[4])
    return 1
  end
  return 0
end
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
local fails = redis.call('INCR', KEYS[1])
if fails == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  redis.call('DEL', KEYS[5])
end
local oks = tonumber(redis.call('GET', KEYS[5]) or '0')
if fails >= tonumber(ARGV[3]) and fails / (fails + oks) >= tonumber(ARGV[6]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[4])
  redis.call('SET', KEYS[3], '1', 'EX', ARGV[5])
  redis.call('DEL', KEYS[1], KEYS[5])
  return 1
end
return 0
"""

# A success counts only while failures are being counted. KEYS: fails, oks
_OK = """
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return 0 end
redis.call('INCR', KEYS[2])
redis.call('PEXPIRE', KEYS[2], ttl)
return 1
"""

# KEYS: fails, open, tripped, probe, oks · ARGV: token
_SUCCEED = """
if redis.call('GET', KEYS[4]) == ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
  return 1
end
return 0
"""

# KEYS: fails, open, tripped, probe, why · ARGV: open_s, tripped_ttl, reason
_QUARANTINE = """
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[2])
redis.call('SET', KEYS[5], ARGV[3], 'EX', ARGV[1])
redis.call('DEL', KEYS[1], KEYS[4])
return 1
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _keys(target: str) -> list[str]:
    base = f"cb:{{{target}}}"
    return [f"{base}:fails", f"{base}:open", f"{base}:tripped", f"{base}:probe"]


def _secs(value: float) -> int:
    return max(1, round(value))  # Redis EX takes whole seconds


class RedisBreakerStore:
    #: After a Redis error, skip Redis (fail open) for this long.
    BACKOFF_SECONDS = 5.0

    def __init__(self, redis: Redis, clock: Callable[[], float] = time.monotonic) -> None:
        self.redis = redis
        self._now = clock
        self._down_until = 0.0
        self._outage = False
        self._decide = redis.register_script(_DECIDE)
        self._fail = redis.register_script(_FAIL)
        self._succeed = redis.register_script(_SUCCEED)
        self._ok = redis.register_script(_OK)
        self._release = redis.register_script(_RELEASE)
        self._quarantine = redis.register_script(_QUARANTINE)

    def _available(self) -> bool:
        return self._now() >= self._down_until

    @property
    def degraded(self) -> bool:
        """Redis is failing: state() answers "closed" (fail open), which may not be true."""
        return self._outage

    def _broken(self, what: str) -> None:
        if not self._outage:  # log when the outage starts, not on every retry
            log.warning("circuit-breaker Redis unavailable (%s); failing open", what)
        self._outage = True
        self._down_until = self._now() + self.BACKOFF_SECONDS

    async def _run(self, script: Any, keys: list[str], args: list[Any], what: str) -> int | None:
        if not self._available():
            return None
        try:
            result = int(await script(keys=keys, args=args))
        except RedisError:
            self._broken(what)
            return None
        if self._outage:
            log.warning("circuit-breaker Redis back")
            self._outage = False
        return result

    async def decide(self, target: str, cfg: BreakerConfig) -> Ticket:
        _, open_, tripped, probe = _keys(target)
        token = uuid.uuid4().hex
        code = await self._run(
            self._decide,
            [open_, tripped, probe],
            [token, _secs(cfg.probe_timeout_seconds)],
            "decide",
        )
        if code == 0:
            return Ticket(Decision.DENY)
        if code == 2:
            return Ticket(Decision.PROBE, token)
        return Ticket(Decision.ALLOW)  # closed, or Redis down → fail open

    async def record_success(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> None:
        if ticket.token is None:
            # Counted (for the failure rate) only while failures are: one cheap call.
            k = _keys(target)
            await self._run(self._ok, [k[0], f"cb:{{{target}}}:oks"], [], "record_success")
            return
        keys = [*_keys(target), f"cb:{{{target}}}:oks"]
        if await self._run(self._succeed, keys, [ticket.token], "record_success"):
            with contextlib.suppress(RedisError):
                await self.redis.delete(f"cb:{{{target}}}:why")

    async def record_failure(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> bool:
        args = [
            ticket.token or "",
            _secs(cfg.window_seconds),
            cfg.failure_threshold,
            _secs(cfg.open_seconds),
            _secs(tripped_ttl(cfg)),
            cfg.failure_rate,
        ]
        keys = [*_keys(target), f"cb:{{{target}}}:oks"]
        return bool(await self._run(self._fail, keys, args, "record_failure"))

    async def release(self, target: str, ticket: Ticket) -> None:
        if ticket.token is not None:
            await self._run(self._release, [_keys(target)[3]], [ticket.token], "release")

    async def quarantine(
        self, target: str, cfg: BreakerConfig, seconds: float, reason: str
    ) -> None:
        """Hold the breaker open for `seconds` (a fault that won't heal soon), with why."""
        keys = [*_keys(target), f"cb:{{{target}}}:why"]
        ttl = seconds + cfg.probe_timeout_seconds + cfg.window_seconds
        await self._run(self._quarantine, keys, [_secs(seconds), _secs(ttl), reason], "quarantine")

    async def reason(self, target: str) -> str | None:
        if not self._available():
            return None
        try:
            raw = await self.redis.get(f"cb:{{{target}}}:why")
        except RedisError:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def state(self, target: str) -> State:
        _, open_, tripped, _ = _keys(target)
        if not self._available():
            return State.CLOSED
        try:
            if await self.redis.exists(open_):
                return State.OPEN
            return State.HALF_OPEN if await self.redis.exists(tripped) else State.CLOSED
        except RedisError:
            self._broken("state")
            return State.CLOSED


# --- memory ----------------------------------------------------------------


class MemoryBreakerStore:
    """Same semantics as the Redis store, for one process. Time via an injectable clock."""

    MAX_TRACKED = 1024  # prune expired entries beyond this many targets

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._now = clock or time.monotonic
        self._fails: dict[str, tuple[int, float]] = {}  # target → (count, window_end)
        self._oks: dict[str, int] = {}  # successes since the window's first failure
        self._open_until: dict[str, float] = {}
        self._tripped_until: dict[str, float] = {}
        self._probe: dict[str, tuple[str, float]] = {}  # target → (token, expires)
        self._why: dict[str, str] = {}  # quarantine reason

    def _tripped(self, target: str, now: float) -> bool:
        return self._tripped_until.get(target, 0) > now

    def _prune(self, now: float) -> None:
        if len(self._fails) + len(self._tripped_until) <= self.MAX_TRACKED:
            return
        self._fails = {t: v for t, v in self._fails.items() if v[1] > now}
        self._tripped_until = {t: u for t, u in self._tripped_until.items() if u > now}
        self._open_until = {t: u for t, u in self._open_until.items() if u > now}
        self._probe = {t: p for t, p in self._probe.items() if p[1] > now}

    def _open(self, target: str, cfg: BreakerConfig, now: float) -> None:
        self._open_until[target] = now + cfg.open_seconds
        self._tripped_until[target] = now + tripped_ttl(cfg)
        self._fails.pop(target, None)
        self._oks.pop(target, None)
        self._probe.pop(target, None)

    async def decide(self, target: str, cfg: BreakerConfig) -> Ticket:
        now = self._now()
        if self._open_until.get(target, 0) > now:
            return Ticket(Decision.DENY)
        if self._tripped(target, now):
            token, expires = self._probe.get(target, ("", 0.0))
            if expires > now:
                return Ticket(Decision.DENY)
            token = uuid.uuid4().hex
            self._probe[target] = (token, now + cfg.probe_timeout_seconds)
            return Ticket(Decision.PROBE, token)
        return Ticket(Decision.ALLOW)

    def _is_probe(self, target: str, ticket: Ticket) -> bool:
        return ticket.token is not None and self._probe.get(target, ("", 0.0))[0] == ticket.token

    async def record_success(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> None:
        if self._is_probe(target, ticket):
            for d in (
                self._fails,
                self._oks,
                self._open_until,
                self._tripped_until,
                self._probe,
                self._why,
            ):
                d.pop(target, None)
            return
        if ticket.token is None and (f := self._fails.get(target)) and f[1] > self._now():
            self._oks[target] = self._oks.get(target, 0) + 1

    async def quarantine(
        self, target: str, cfg: BreakerConfig, seconds: float, reason: str
    ) -> None:
        now = self._now()
        self._open_until[target] = now + seconds
        self._tripped_until[target] = now + seconds + cfg.probe_timeout_seconds + cfg.window_seconds
        self._fails.pop(target, None)
        self._probe.pop(target, None)
        self._why[target] = reason

    async def reason(self, target: str) -> str | None:
        return self._why.get(target) if self._tripped(target, self._now()) else None

    async def record_failure(self, target: str, cfg: BreakerConfig, ticket: Ticket) -> bool:
        now = self._now()
        if ticket.token is not None:
            if self._is_probe(target, ticket):
                self._open(target, cfg, now)
                return True
            return False
        if self._tripped(target, now):
            return False  # already open/half-open: stragglers don't change anything
        self._prune(now)
        count, window_end = self._fails.get(target, (0, now + cfg.window_seconds))
        if window_end <= now or count == 0:
            count, window_end = 0, now + cfg.window_seconds
            self._oks.pop(target, None)
        count += 1
        oks = self._oks.get(target, 0)
        if count >= cfg.failure_threshold and count / (count + oks) >= cfg.failure_rate:
            self._open(target, cfg, now)
            return True
        self._fails[target] = (count, window_end)
        return False

    async def release(self, target: str, ticket: Ticket) -> None:
        if self._is_probe(target, ticket):
            self._probe.pop(target, None)

    async def state(self, target: str) -> State:
        now = self._now()
        if self._open_until.get(target, 0) > now:
            return State.OPEN
        return State.HALF_OPEN if self._tripped(target, now) else State.CLOSED
