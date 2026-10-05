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
import os
import re
import sys

from sqlalchemy import text

from app import config
from app.db import make_engine

# api_keys columns dashboards may read: everything except key_hash.
READABLE_KEY_COLUMNS = ("id", "name", "prefix", "tier", "team", "created_at", "revoked_at")
_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


async def prune(days: int, batch: int = 10_000) -> int:
    """Delete rows older than `days`, `batch` at a time (short transactions, no long locks)."""
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
    """A SQL string literal. DDL like CREATE ROLE … PASSWORD can't take bind parameters."""
    if "\x00" in value:
        raise ValueError("password contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


async def grant_readonly(role: str, password: str) -> None:
    if not _ROLE.fullmatch(role):
        raise ValueError(f"role name {role!r} must match {_ROLE.pattern}")
    if len(password) < 16:
        raise ValueError("READONLY_PASSWORD must be at least 16 characters")
    engine = make_engine(config.settings.database_url, timeout=60)
    try:
        async with engine.begin() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            )
            verb = "ALTER" if exists else "CREATE"
            await conn.execute(text(f"{verb} ROLE {role} LOGIN PASSWORD {_literal(password)}"))
            db = await conn.scalar(text("SELECT current_database()"))
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {role}'))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"))
            await conn.execute(text(f"GRANT SELECT ON usage_log TO {role}"))
            cols = ", ".join(READABLE_KEY_COLUMNS)
            await conn.execute(text(f"GRANT SELECT ({cols}) ON api_keys TO {role}"))
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
