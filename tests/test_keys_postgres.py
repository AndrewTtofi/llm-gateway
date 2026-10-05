"""PostgresKeyStore against a real Postgres, schema created by the Alembic migration."""

import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.auth import CachedKeys, PostgresKeyStore, hash_key
from app.db import make_engine, make_sessions

URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test"
)


async def ensure_database() -> None:
    admin_url, _, db = URL.rpartition("/")
    engine = make_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{db}"'))
    finally:
        await engine.dispose()


@pytest.fixture
async def store() -> AsyncIterator[PostgresKeyStore]:
    try:
        await ensure_database()
    except Exception:
        if os.environ.get("REQUIRE_POSTGRES"):
            raise
        pytest.skip("no Postgres at TEST_DATABASE_URL (make up); CI sets REQUIRE_POSTGRES")
    engine = make_engine(URL)
    async with engine.begin() as conn:  # the real migration, not create_all

        def upgrade(sync_conn: object) -> None:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(upgrade)
        await conn.execute(text("TRUNCATE api_keys"))
    yield PostgresKeyStore(make_sessions(engine))
    await engine.dispose()


async def test_create_lookup_list_revoke(store: PostgresKeyStore) -> None:
    key, plaintext = await store.create(
        "svc", "standard", {"requests_per_minute": 7, "allowed_aliases": ["fast"]}
    )
    assert plaintext.startswith("gw_") and key.prefix == plaintext[:8]
    found = await store.get_by_hash(hash_key(plaintext))
    assert found is not None and found.id == key.id
    assert found.overrides == {"requests_per_minute": 7, "allowed_aliases": ["fast"]}
    assert [k.id for k in await store.list()] == [key.id]
    assert await store.revoke(key.id) is True
    assert await store.revoke(key.id) is False  # already revoked
    revoked = await store.get_by_hash(hash_key(plaintext))
    assert revoked is not None and revoked.revoked


async def test_plaintext_is_never_stored(store: PostgresKeyStore) -> None:
    _, plaintext = await store.create("svc", "dev", {})
    async with store.sessions() as s:
        row = (await s.execute(text("SELECT * FROM api_keys"))).mappings().one()
    assert plaintext not in str(dict(row))
    assert row["key_hash"] == hash_key(plaintext)


async def test_revoked_keys_dont_authenticate(store: PostgresKeyStore) -> None:
    key, plaintext = await store.create("svc", "dev", {})
    cached = CachedKeys(store)
    assert await cached.authenticate(plaintext) is not None
    await store.revoke(key.id)
    cached.invalidate()
    assert await cached.authenticate(plaintext) is None


async def test_revoke_unknown_or_malformed_id(store: PostgresKeyStore) -> None:
    assert await store.revoke("not-a-uuid") is False
    assert await store.revoke("00000000-0000-0000-0000-000000000000") is False
