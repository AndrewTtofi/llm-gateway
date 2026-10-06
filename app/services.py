"""Process-wide stores: API keys, rate limiter, spend, circuit breaker.

They start as in-memory implementations (tests, `GATEWAY_STORES=memory`) and are
switched to Redis/Postgres at startup (`start()`), so modules always read them from
here rather than holding their own reference.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import config
from app.auth import CachedKeys, MemoryKeyStore, PostgresKeyStore
from app.cache import CacheStore, MemoryCacheStore, RedisCacheStore
from app.db import make_engine, make_sessions
from app.judge import Judge, MemoryScoreStore, PostgresScoreStore
from app.observability.usage import MemoryUsageSink, PostgresUsageWriter, UsageSink
from app.ratelimit import (
    Concurrency,
    Limiter,
    MemoryLimiter,
    MemorySpend,
    RedisLimiter,
    RedisSpend,
    SpendTracker,
)
from app.routing import router
from app.routing.breaker import MemoryBreakerStore, RedisBreakerStore

keys: CachedKeys = CachedKeys(MemoryKeyStore())
limiter: Limiter = MemoryLimiter()
spend: SpendTracker = MemorySpend()
concurrency = Concurrency()  # per replica, so never replaced (ADR 0023)
usage: UsageSink = MemoryUsageSink()
response_cache: CacheStore = MemoryCacheStore()  # ADR 0018
judge: Judge = Judge(MemoryScoreStore())  # ADR 0022
_writer: PostgresUsageWriter | None = None

log = logging.getLogger(__name__)

_redis: Redis | None = None
_cache_redis: Redis | None = None  # CACHE_REDIS_URL, when the cache has its own
_engine: AsyncEngine | None = None


async def start() -> None:
    global keys, limiter, spend, usage, response_cache, judge, _redis, _engine, _writer, started
    global _cache_redis
    if config.settings.gateway_stores == "memory":
        router.store = MemoryBreakerStore()
        judge.start()
        started = True
        return
    timeout = config.registry.circuit_breaker.redis_timeout_ms / 1000
    _redis = Redis.from_url(
        config.settings.redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
    )
    _engine = make_engine(config.settings.database_url, timeout=config.settings.db_timeout_seconds)
    sessions = make_sessions(_engine)
    keys = CachedKeys(PostgresKeyStore(sessions))
    _writer = PostgresUsageWriter(sessions)
    _writer.start()
    usage = _writer
    limiter = RedisLimiter(_redis)
    if config.settings.cache_redis_url:
        _cache_redis = Redis.from_url(
            config.settings.cache_redis_url, socket_timeout=timeout, socket_connect_timeout=timeout
        )
    response_cache = RedisCacheStore(_cache_redis or _redis)
    judge = Judge(PostgresScoreStore(sessions))
    judge.start()
    spend = RedisSpend(_redis)
    router.store = (
        MemoryBreakerStore()
        if config.registry.circuit_breaker.store == "memory"
        else RedisBreakerStore(_redis)
    )
    global _deps, _deps_task
    _deps = await check_dependencies()
    _deps_task = asyncio.create_task(_refresh_dependencies())
    started = True


async def stop() -> None:
    global _redis, _cache_redis, _engine, _writer, _deps_task, started
    started = False
    await judge.stop()
    if _deps_task is not None:
        _deps_task.cancel()
        _deps_task = None
    if _writer is not None:
        await _writer.stop()  # flush queued usage rows before the engine goes
        _writer = None
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _cache_redis is not None:
        await _cache_redis.aclose()
        _cache_redis = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None


started = False  # set once start() has finished


KEYS_CHANNEL = "gateway:keys-changed"


async def announce_key_change() -> None:
    """Tell every replica to drop its cached keys now (revoked or edited), instead of
    within the cache's 30 s (ADR 0023). Best effort: the TTL still bounds it."""
    if _redis is None:
        return
    try:
        await _redis.publish(KEYS_CHANNEL, "1")
    except Exception as exc:
        log.warning("couldn't announce a key change: %s", type(exc).__name__)


async def watch_key_changes() -> None:
    """Drop cached keys when another replica announces a change. Its own connection: a
    subscriber waits indefinitely, which the request path's short socket timeout forbids."""
    if config.settings.gateway_stores == "memory":
        return
    while True:
        # Keepalive and periodic PINGs: a half-open connection (NAT timeout, failover
        # without a reset) is noticed instead of waiting forever.
        client = Redis.from_url(
            config.settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=10,
            socket_keepalive=True,
            health_check_interval=30,
        )
        try:
            async with client.pubsub() as ps:
                await ps.subscribe(KEYS_CHANNEL)
                while True:
                    message = await ps.get_message(timeout=1.0)
                    # "subscribe" arrives on every (re)subscription, including redis-py's
                    # own reconnects: changes may have been missed, so drop the cache too.
                    if message and message.get("type") in ("message", "subscribe"):
                        keys.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("key change subscription lost: %s", type(exc).__name__)
        finally:
            await client.aclose()
        await asyncio.sleep(5)


def redis_client() -> Redis | None:
    """The shared Redis client (None in memory mode), for features that need their own keys."""
    return _redis


# /readyz reads a cached status, refreshed in the background, so the probe never waits on
# I/O: a hung Postgres must not make every replica's probe time out at once, and probe
# traffic must not take connections from the request path's pool.
DEPENDENCY_CHECK_INTERVAL = 5.0
DEPENDENCY_CHECK_TIMEOUT = 0.5
_deps: dict[str, str] = {}
_deps_task: asyncio.Task[None] | None = None


async def check_dependencies(limit_s: float = DEPENDENCY_CHECK_TIMEOUT) -> dict[str, str]:
    """Probe Redis and Postgres concurrently, each bounded by `limit_s`."""
    timeout = limit_s
    redis, engine = _redis, _engine  # stop() may clear them meanwhile

    async def ping_redis(r: Redis) -> str:
        try:
            await asyncio.wait_for(r.ping(), timeout)
            return "ok"
        except Exception:
            return "unreachable"

    async def ping_postgres(e: AsyncEngine) -> str:
        try:
            async with asyncio.timeout(timeout), e.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return "ok"
        except Exception:
            return "unreachable"

    checks: dict[str, Any] = {}
    if redis is not None:
        checks["redis"] = ping_redis(redis)
    if engine is not None:
        checks["postgres"] = ping_postgres(engine)
    results = await asyncio.gather(*checks.values())
    return dict(zip(checks, results, strict=True))


async def _refresh_dependencies() -> None:
    global _deps
    while True:
        _deps = await check_dependencies()
        await asyncio.sleep(DEPENDENCY_CHECK_INTERVAL)


def dependencies() -> dict[str, str]:
    """Last known reachability of shared dependencies, for /readyz. Informational: the
    gateway keeps serving without them (limits fail open, cached keys keep working)."""
    return dict(_deps)


def status() -> dict[str, Any]:
    return {"stores": config.settings.gateway_stores}
