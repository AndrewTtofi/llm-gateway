"""Operator tasks, run as one-off jobs (not on the request path).

python -m app.maintenance prune --days 90
    Delete usage_log rows older than N days, in batches, so the table (one row per
    request) doesn't grow forever. Spend history is in usage_log only: export what
    you need for finance before pruning it.

python -m app.maintenance grant-readonly --role grafana_ro
    Create or update a read-only login for dashboards. It can read usage_log and the
    non-secret columns of api_keys — never key_hash. Password from $READONLY_PASSWORD.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import sys

from sqlalchemy import text

from app import config
from app.db import make_engine

# api_keys columns dashboards may read: everything except key_hash.
READABLE_KEY_COLUMNS = ("id", "name", "prefix", "tier", "team", "created_at", "revoked_at")
_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


async def prune(days: int, batch: int = 10_000) -> int:
    """Delete rows older than `days`, `batch` at a time (short transactions, no long locks)."""
    if days < 1 or batch < 1:
        raise ValueError("days and batch must be at least 1")
    engine = make_engine(config.settings.database_url, timeout=60)
    deleted = 0
    try:
        while True:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "DELETE FROM usage_log WHERE id IN (SELECT id FROM usage_log "
                        "WHERE created_at < now() - make_interval(days => :days) LIMIT :batch)"
                    ),
                    {"days": days, "batch": batch},
                )
            count = int(getattr(result, "rowcount", 0) or 0)
            deleted += count
            if count < batch:
                return deleted
    finally:
        await engine.dispose()


def _literal(value: str) -> str:
    """A SQL string literal. Only used for values made of base64 and `$:` characters
    (the SCRAM verifier below), so there's nothing to escape; checked anyway."""
    if not re.fullmatch(r"[A-Za-z0-9+/=$:\-]*", value):
        raise ValueError("unexpected characters in a SQL literal")
    return "'" + value + "'"


def _ident(name: str) -> str:
    """A quoted SQL identifier (works for reserved words like `user` too)."""
    return '"' + name.replace('"', '""') + '"'


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for a password (RFC 5802/7677).
    Sending it instead of the password keeps the plaintext out of server logs
    (log_statement = ddl) and pg_stat_activity, and leaves nothing to escape."""
    if "\x00" in password:
        raise ValueError("password contains a NUL byte")
    if not password.isascii():
        # PostgreSQL applies SASLprep (RFC 4013) to non-ASCII passwords before hashing; a
        # verifier built without it wouldn't match, and the role couldn't log in.
        raise ValueError("password must be ASCII (generate one with `openssl rand -hex 24`)")
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


async def grant_readonly(role: str, password: str) -> None:
    if not _ROLE.fullmatch(role):
        raise ValueError(f"role name {role!r} must match {_ROLE.pattern}")
    if len(password) < 16:
        raise ValueError("READONLY_PASSWORD must be at least 16 characters")
    verifier = _literal(scram_verifier(password))
    r = _ident(role)
    engine = make_engine(config.settings.database_url, timeout=60)
    try:
        async with engine.begin() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            )
            verb = "ALTER" if exists else "CREATE"
            # DDL goes to the driver as-is: no bind parameters exist for it, and `text()`
            # would read the verifier's `:…` as one.
            await conn.exec_driver_sql(f"{verb} ROLE {r} LOGIN PASSWORD {verifier}")
            db = str(await conn.scalar(text("SELECT current_database()")))
            await conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {_ident(db)} TO {r}")
            await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {r}")
            await conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {r}")
            await conn.exec_driver_sql(f"GRANT SELECT ON usage_log TO {r}")
            cols = ", ".join(READABLE_KEY_COLUMNS)
            await conn.exec_driver_sql(f"GRANT SELECT ({cols}) ON api_keys TO {r}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prune", help="delete old usage_log rows")
    pr.add_argument("--days", type=int, required=True)
    pr.add_argument("--batch", type=int, default=10_000)
    gr = sub.add_parser("grant-readonly", help="read-only login for dashboards")
    gr.add_argument("--role", default="grafana_ro")
    args = p.parse_args(argv)

    if args.cmd == "prune":
        if args.days < 1:
            p.error("--days must be at least 1")
        if args.batch < 1:
            p.error("--batch must be at least 1")
        deleted = asyncio.run(prune(args.days, args.batch))
        print(f"deleted {deleted} usage_log rows older than {args.days} days")
        return 0
    password = os.environ.get("READONLY_PASSWORD", "")
    asyncio.run(grant_readonly(args.role, password))
    cols = ", ".join(READABLE_KEY_COLUMNS)
    print(f"role {args.role}: read-only access to usage_log and api_keys ({cols})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
