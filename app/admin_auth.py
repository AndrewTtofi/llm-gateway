"""Operator credentials, the admin audit log, and failed-login limiting (ADR 0025).

Operators each have their own admin key, so every change can be traced to a person and one
person can be removed without rotating everyone's key:

- `GATEWAY_ADMIN_KEY`: one key, the operator `admin` (the original setup, still works).
- `ADMIN_KEYS_FILE`: one operator per line, `name sha256-hex-of-their-key`. The file holds
  hashes, never keys, so it can live in a config repo. `make admin-key name=…` prints both.

Admin keys are long random strings, so a plain SHA-256 is enough (as for API keys). The
file is re-read when it changes: adding or removing an operator needs no restart.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import config
from app.db import AdminAuditRow
from app.observability import logging as obs_log

log = logging.getLogger(__name__)

_LINE = re.compile(r"^([A-Za-z0-9._@-]{1,64})\s+([0-9a-f]{64})$")
_file_cache: tuple[str, float, dict[str, str]] = ("", 0.0, {})


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()


def _from_file(path: str) -> dict[str, str]:
    global _file_cache
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        log.error("ADMIN_KEYS_FILE %s can't be read; only GATEWAY_ADMIN_KEY works", path)
        return {}
    if _file_cache[0] == path and _file_cache[1] == mtime:
        return _file_cache[2]
    operators: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if m := _LINE.match(line):
                operators[m.group(1)] = m.group(2)
            else:
                log.error("ADMIN_KEYS_FILE line %d isn't `name sha256-hex`; ignored", n)
    _file_cache = (path, mtime, operators)
    return operators


def operators() -> dict[str, str]:
    """Operator name → hash of their admin key."""
    ops: dict[str, str] = {}
    if config.settings.gateway_admin_key:
        ops["admin"] = key_hash(config.settings.gateway_admin_key)
    if config.settings.admin_keys_file:
        ops |= _from_file(config.settings.admin_keys_file)
    return ops


def authenticate(token: str) -> str | None:
    """The operator this admin key belongs to, or None. Compares every entry in constant
    time, so timing doesn't reveal which (or whether a) hash came close."""
    if not token:
        return None
    presented = key_hash(token)
    found = None
    for name, digest in operators().items():
        if hmac.compare_digest(presented, digest):
            found = name
    return found


class FailedLogins:
    """At most `limit` failed admin logins per source per `window` seconds, per replica.
    Admin keys can't be guessed, so this is about noise and early warning; every failure
    is logged."""

    def __init__(self, limit: int = 10, window: float = 60.0, max_sources: int = 10_000):
        self.limit, self.window, self.max_sources = limit, window, max_sources
        self._seen: OrderedDict[str, tuple[float, int]] = OrderedDict()  # source → (start, n)

    def blocked(self, source: str) -> bool:
        start, n = self._seen.get(source, (0.0, 0))
        return n >= self.limit and time.monotonic() - start < self.window

    def failed(self, source: str) -> None:
        now = time.monotonic()
        start, n = self._seen.pop(source, (now, 0))
        if now - start >= self.window:
            start, n = now, 0
        self._seen[source] = (start, n + 1)
        while len(self._seen) > self.max_sources:
            self._seen.popitem(last=False)


failed_logins = FailedLogins()


# --- audit log -------------------------------------------------------------------------


@dataclass
class AuditEntry:
    at: datetime
    operator: str
    action: str  # key.create, key.update, key.revoke, config.reload
    target: str | None
    detail: dict[str, Any]


class AuditStore(Protocol):
    async def save(self, entry: AuditEntry) -> None: ...
    async def recent(self, limit: int) -> list[AuditEntry]: ...


class MemoryAuditStore:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def save(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    async def recent(self, limit: int) -> list[AuditEntry]:
        return list(reversed(self.entries[-limit:]))


class PostgresAuditStore:
    def __init__(self, sessions: async_sessionmaker[Any]) -> None:
        self.sessions = sessions

    async def save(self, entry: AuditEntry) -> None:
        async with self.sessions() as s, s.begin():
            await s.execute(insert(AdminAuditRow).values(**vars(entry)))

    async def recent(self, limit: int) -> list[AuditEntry]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(AdminAuditRow).order_by(AdminAuditRow.id.desc()).limit(limit)
            )
            return [
                AuditEntry(r.at, r.operator, r.action, r.target, dict(r.detail or {})) for r in rows
            ]


async def record(operator: str, action: str, target: str | None = None, **detail: Any) -> None:
    """Log and store one admin change. Never raises: an audit outage mustn't block the
    change itself, but it's logged as an error."""
    from app import services

    entry = AuditEntry(datetime.now(UTC), operator, action, target, detail)
    obs_log.audit.info("admin", operator=operator, action=action, target=target, **detail)
    try:
        await services.audit.save(entry)
    except Exception as exc:
        log.error("admin audit row not written (%s): %s %s", type(exc).__name__, action, target)


def admin_key_line(name: str) -> tuple[str, str]:
    """A new operator key and the ADMIN_KEYS_FILE line for it (`make admin-key`)."""
    import secrets

    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", name):
        raise ValueError("operator names: letters, digits and . _ @ -, up to 64 characters")
    key = secrets.token_hex(32)
    return key, f"{name} {key_hash(key)}"


if __name__ == "__main__":  # python -m app.admin_auth <name>
    import sys

    new_key, line = admin_key_line(sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"operator key (give it to them, it isn't stored): {new_key}")
    print(f"line for ADMIN_KEYS_FILE:                       {line}")
