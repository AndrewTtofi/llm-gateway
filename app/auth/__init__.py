"""Gateway API keys: generation, hashing, storage and lookup (ADR 0006)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Limits
from app.db import ApiKeyRow

KEY_PREFIX = "gw_"
# gw_ + token_urlsafe(32): exactly 43 url-safe base64 characters.
KEY_FORMAT = re.compile(r"gw_[A-Za-z0-9_-]{43}")
log = logging.getLogger(__name__)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)  # 256 random bits


def hash_key(key: str) -> str:
    # Fast hash is fine: the key is 256 random bits, not a guessable password (ADR 0006).
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class EffectiveLimits:
    requests_per_minute: int
    tokens_per_minute: int
    monthly_budget_usd: float
    allowed_aliases: tuple[str, ...]
    concurrent_requests: int = 0  # in flight per replica; 0 = no limit

    def allows(self, model: str) -> bool:
        return "*" in self.allowed_aliases or model in self.allowed_aliases


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    prefix: str
    tier: str
    overrides: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    revoked: bool = False
    team: str | None = None  # teams have their own monthly budget (limits.yaml)

    @property
    def team_spend_id(self) -> str | None:
        """Where the team's month-to-date spend is tracked (same store as keys)."""
        return f"team:{self.team}" if self.team else None

    def limits(self, limits: Limits) -> EffectiveLimits:
        tier = limits.tiers[self.tier]
        o = self.overrides
        allowed = o.get("allowed_aliases")
        return EffectiveLimits(
            requests_per_minute=o.get("requests_per_minute") or tier.requests_per_minute,
            tokens_per_minute=o.get("tokens_per_minute") or tier.tokens_per_minute,
            monthly_budget_usd=(
                o["monthly_budget_usd"]
                if o.get("monthly_budget_usd") is not None
                else tier.monthly_budget_usd
            ),
            allowed_aliases=tuple(allowed if allowed is not None else tier.allowed_aliases),
            concurrent_requests=tier.concurrent_requests,
        )

    def public(self) -> dict[str, Any]:
        """Safe to return from the admin API: never the key or its hash."""
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "tier": self.tier,
            "team": self.team,
            "overrides": self.overrides,
            "revoked": self.revoked,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


OVERRIDES = ("requests_per_minute", "tokens_per_minute", "monthly_budget_usd", "allowed_aliases")


class KeyStore(Protocol):
    async def create(
        self, name: str, tier: str, overrides: dict[str, Any]
    ) -> tuple[ApiKey, str]: ...
    async def get_by_hash(self, key_hash: str) -> ApiKey | None: ...
    async def list(self) -> list[ApiKey]: ...
    async def revoke(self, key_id: str) -> bool: ...
    async def update(self, key_id: str, changes: dict[str, Any]) -> ApiKey | None: ...


EDITABLE = ("name", "tier", "team", *OVERRIDES)


def _apply(key: ApiKey, changes: dict[str, Any]) -> ApiKey:
    """`changes` maps EDITABLE fields to new values; None clears a field or override."""
    overrides = dict(key.overrides)
    for k in OVERRIDES:
        if k in changes:
            if changes[k] is None:
                overrides.pop(k, None)
            else:
                overrides[k] = changes[k]
    return replace(
        key,
        name=changes.get("name") or key.name,
        tier=changes.get("tier") or key.tier,
        team=changes["team"] if "team" in changes else key.team,
        overrides=overrides,
    )


class MemoryKeyStore:
    """For tests and single-process dev without Postgres."""

    def __init__(self) -> None:
        self._by_hash: dict[str, ApiKey] = {}

    async def create(self, name: str, tier: str, overrides: dict[str, Any]) -> tuple[ApiKey, str]:
        plaintext = generate_key()
        overrides = dict(overrides)
        team = overrides.pop("team", None)
        key = ApiKey(
            id=str(uuid.uuid4()),
            name=name,
            prefix=plaintext[:8],
            tier=tier,
            overrides={k: v for k, v in overrides.items() if v is not None},
            created_at=datetime.now(UTC),
            team=team,
        )
        self._by_hash[hash_key(plaintext)] = key
        return key, plaintext

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._by_hash.get(key_hash)

    async def list(self) -> list[ApiKey]:
        return list(self._by_hash.values())

    async def revoke(self, key_id: str) -> bool:
        for h, key in self._by_hash.items():
            if key.id == key_id and not key.revoked:
                self._by_hash[h] = replace(key, revoked=True)
                return True
        return False

    async def update(self, key_id: str, changes: dict[str, Any]) -> ApiKey | None:
        for h, key in self._by_hash.items():
            if key.id == key_id and not key.revoked:
                self._by_hash[h] = _apply(key, changes)
                return self._by_hash[h]
        return None


def _from_row(row: ApiKeyRow) -> ApiKey:
    overrides = {k: getattr(row, k) for k in OVERRIDES if getattr(row, k) is not None}
    return ApiKey(
        id=str(row.id),
        name=row.name,
        prefix=row.prefix,
        tier=row.tier,
        overrides=overrides,
        created_at=row.created_at,
        revoked=row.revoked_at is not None,
        team=row.team,
    )


class PostgresKeyStore:
    def __init__(self, sessions: async_sessionmaker[Any]) -> None:
        self.sessions = sessions

    async def create(self, name: str, tier: str, overrides: dict[str, Any]) -> tuple[ApiKey, str]:
        plaintext = generate_key()
        row = ApiKeyRow(
            name=name,
            prefix=plaintext[:8],
            key_hash=hash_key(plaintext),
            tier=tier,
            team=overrides.get("team"),
            **{k: overrides[k] for k in OVERRIDES if overrides.get(k) is not None},
        )
        async with self.sessions() as s, s.begin():
            s.add(row)
        async with self.sessions() as s:
            fresh = await s.get(ApiKeyRow, row.id)
            assert fresh is not None
            return _from_row(fresh), plaintext

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        async with self.sessions() as s:
            row = (
                await s.execute(select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash))
            ).scalar_one_or_none()
            return _from_row(row) if row else None

    async def list(self) -> list[ApiKey]:
        async with self.sessions() as s:
            rows = (await s.execute(select(ApiKeyRow).order_by(ApiKeyRow.created_at))).scalars()
            return [_from_row(r) for r in rows]

    async def revoke(self, key_id: str) -> bool:
        try:
            kid = uuid.UUID(key_id)
        except ValueError:
            return False
        async with self.sessions() as s, s.begin():
            result = await s.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == kid, ApiKeyRow.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
            return bool(getattr(result, "rowcount", 0))

    async def update(self, key_id: str, changes: dict[str, Any]) -> ApiKey | None:
        try:
            kid = uuid.UUID(key_id)
        except ValueError:
            return None
        values = {k: v for k, v in changes.items() if k in EDITABLE}
        if not values:
            async with self.sessions() as s:
                row = await s.get(ApiKeyRow, kid)
                return _from_row(row) if row and row.revoked_at is None else None
        async with self.sessions() as s, s.begin():
            result = await s.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == kid, ApiKeyRow.revoked_at.is_(None))
                .values(**values)
            )
            if not getattr(result, "rowcount", 0):
                return None
        async with self.sessions() as s:
            row = await s.get(ApiKeyRow, kid)
            return _from_row(row) if row else None


class CachedKeys:
    """Per-process lookup cache in front of a KeyStore (ADR 0006).

    - Only well-formed keys reach the store: random garbage costs no query.
    - Hits and misses are cached in separate bounded LRUs, so a flood of unknown keys
      can't evict the valid ones.
    - Concurrent lookups of the same key share one query.
    - If the store is down, a key seen within `stale_ttl` keeps working (stale-if-error);
      unknown keys are rejected (fail closed).
    Revocation is visible within `ttl`.
    """

    def __init__(
        self,
        store: KeyStore,
        ttl: float = 30.0,
        stale_ttl: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
    ) -> None:
        self.store, self.ttl, self.stale_ttl, self._now = store, ttl, stale_ttl, clock
        self._max = max_entries
        self._hits: OrderedDict[str, tuple[ApiKey, float]] = OrderedDict()  # → fetched at
        self._misses: OrderedDict[str, float] = OrderedDict()  # → expires at
        self._inflight: dict[str, asyncio.Task[ApiKey | None]] = {}

    @staticmethod
    def _bounded_put(cache: OrderedDict[str, Any], key: str, value: Any, limit: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    async def authenticate(self, presented: str) -> ApiKey | None:
        if not KEY_FORMAT.fullmatch(presented):
            return None
        h = hash_key(presented)
        now = self._now()
        if self._misses.get(h, 0) > now:
            return None
        hit = self._hits.get(h)
        if hit is not None and now - hit[1] < self.ttl:
            self._hits.move_to_end(h)
            return None if hit[0].revoked else hit[0]

        task = self._inflight.get(h)
        if task is None:
            task = asyncio.ensure_future(self.store.get_by_hash(h))
            self._inflight[h] = task
            task.add_done_callback(lambda _: self._inflight.pop(h, None))
        try:
            key = await asyncio.shield(task)
        except Exception:
            if hit is not None and now - hit[1] < self.stale_ttl:
                log.warning("key store unavailable; serving a cached key (stale-if-error)")
                from app.observability import metrics  # local: avoid an import cycle

                metrics.auth_stale.inc()
                return None if hit[0].revoked else hit[0]
            raise
        if key is None:
            self._bounded_put(self._misses, h, now + self.ttl, self._max)
            self._hits.pop(h, None)
            return None
        self._bounded_put(self._hits, h, (key, now), self._max)
        return None if key.revoked else key

    def invalidate(self) -> None:
        self._hits.clear()
        self._misses.clear()
