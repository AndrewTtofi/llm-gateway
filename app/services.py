"""Process-wide stores: API keys, rate limiter, spend, circuit breaker.

They start as in-memory implementations (tests, `GATEWAY_STORES=memory`) and are
switched to Redis/Postgres at startup (`start()`), so modules always read them from
here rather than holding their own reference.
"""

from __future__ import annotations

import asyncio
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import config
from app.auth import CachedKeys, MemoryKeyStore, PostgresKeyStore
from app.db import make_engine, make_sessions
from app.observability.usage import MemoryUsageSink, PostgresUsageWriter, UsageSink
from app.ratelimit import (
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
usage: UsageSink = MemoryUsageSink()
_writer: PostgresUsageWriter | None = None

_redis: Redis | None = None
_engine: AsyncEngine | None = None


async def start() -> None:
    global keys, limiter, spend, usage, _redis, _engine, _writer, started
    if config.settings.gateway_stores == "memory":
        router.store = MemoryBreakerStore()
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
    spend = RedisSpend(_redis)
    router.store = (
        MemoryBreakerStore()
        if config.registry.circuit_breaker.store == "memory"
        else RedisBreakerStore(_redis)
    )
    started = True


async def stop() -> None:
    global _redis, _engine, _writer, started
    started = False
    if _writer is not None:
        await _writer.stop()  # flush queued usage rows before the engine goes
        _writer = None
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None


started = False  # set once start() has finished


async def dependencies() -> dict[str, str]:
    """Reachability of shared dependencies, for /readyz. Informational: the gateway keeps
    serving without them (limits fail open, cached keys keep working)."""
    out: dict[str, str] = {}
    timeout = config.settings.db_timeout_seconds
    if _redis is not None:
        try:
            await asyncio.wait_for(_redis.ping(), timeout)
            out["redis"] = "ok"
        except Exception:
            out["redis"] = "unreachable"
    if _engine is not None:
        try:
            async with asyncio.timeout(timeout), _engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            out["postgres"] = "ok"
        except Exception:
            out["postgres"] = "unreachable"
    return out


def status() -> dict[str, Any]:
    return {"stores": config.settings.gateway_stores}
