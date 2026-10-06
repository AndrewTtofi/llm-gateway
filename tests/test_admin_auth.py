"""Operator credentials, the admin audit log, failed-login limiting (ADR 0025)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import admin_auth, config, main, services
from app.admin_auth import AuditEntry, FailedLogins, MemoryAuditStore, key_hash
from app.config import Registry
from tests.test_keys_postgres import store  # noqa: F401 — fixture: a migrated test DB

ALICE, BOB = "a" * 64, "b" * 64


@pytest.fixture
def operators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "admins"
    path.write_text(
        f"# operators\nalice {key_hash(ALICE)}\nbob {key_hash(BOB)}  # on call\nnot a valid line\n"
    )
    monkeypatch.setattr(config.settings, "gateway_admin_key", "legacy-admin-key-" + "x" * 20)
    monkeypatch.setattr(config.settings, "admin_keys_file", str(path))
    monkeypatch.setattr(services, "audit", MemoryAuditStore())
    monkeypatch.setattr(admin_auth, "failed_logins", FailedLogins(limit=3, window=60))
    return path


@pytest.fixture
def admin(operators: Path, registry: Registry) -> Iterator[TestClient]:
    with TestClient(main.app) as c:
        yield c


def h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_each_operator_has_their_own_key(operators: Path) -> None:
    assert admin_auth.authenticate(ALICE) == "alice"
    assert admin_auth.authenticate(BOB) == "bob"
    assert admin_auth.authenticate(config.settings.gateway_admin_key) == "admin"
    assert admin_auth.authenticate("c" * 64) is None
    assert admin_auth.authenticate("") is None


def test_the_operators_file_is_reread_when_it_changes(operators: Path) -> None:
    assert admin_auth.authenticate(BOB) == "bob"
    operators.write_text(f"alice {key_hash(ALICE)}\n")  # bob leaves
    os.utime(operators, (time.time() + 5, time.time() + 5))
    assert admin_auth.authenticate(BOB) is None
    assert admin_auth.authenticate(ALICE) == "alice"


def test_admin_changes_are_audited_with_the_operator(admin: TestClient) -> None:
    created = admin.post("/admin/keys", headers=h(ALICE), json={"name": "app", "tier": "dev"})
    assert created.status_code == 201
    key_id = created.json()["id"]
    admin.patch(f"/admin/keys/{key_id}", headers=h(BOB), json={"tokens_per_minute": 9000})
    admin.delete(f"/admin/keys/{key_id}", headers=h(ALICE))
    admin.post("/admin/reload", headers=h(BOB))
    log = admin.get("/admin/audit", headers=h(ALICE)).json()["data"]
    assert [(e["operator"], e["action"]) for e in log] == [
        ("bob", "config.reload"),
        ("alice", "key.revoke"),
        ("bob", "key.update"),
        ("alice", "key.create"),
    ]
    assert log[2]["detail"]["changes"] == {"tokens_per_minute": 9000}
    assert log[3]["target"] == key_id and "key" not in log[3]["detail"]  # never the secret


def test_failed_admin_logins_are_limited(admin: TestClient) -> None:
    for _ in range(3):
        assert admin.get("/admin/keys", headers=h("wrong")).status_code == 401
    blocked = admin.get("/admin/keys", headers=h(ALICE))
    assert blocked.status_code == 429  # even a right key, from this source, for a minute
    assert blocked.json()["error"]["code"] == "admin_login_limited"


def test_failed_login_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(admin_auth.time, "monotonic", lambda: now[0])
    fl = FailedLogins(limit=2, window=60)
    fl.failed("1.2.3.4")
    fl.failed("1.2.3.4")
    assert fl.blocked("1.2.3.4") and not fl.blocked("5.6.7.8")
    now[0] += 61
    assert not fl.blocked("1.2.3.4")


def test_new_operator_keys(capsys: pytest.CaptureFixture[str]) -> None:
    key, line = admin_auth.admin_key_line("carol")
    assert line == f"carol {key_hash(key)}" and len(key) == 64
    with pytest.raises(ValueError):
        admin_auth.admin_key_line("not ok!")


async def test_audit_rows_in_postgres(store: Any) -> None:  # noqa: F811 — migrates the DB
    from datetime import UTC, datetime

    from sqlalchemy import text

    from app.db import make_engine, make_sessions
    from tests.test_keys_postgres import URL

    engine = make_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM admin_audit LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("no migrated test database (make up)")
    audit = admin_auth.PostgresAuditStore(make_sessions(engine))
    entry: dict[str, Any] = {"changes": {"tier": "standard"}}
    await audit.save(AuditEntry(datetime.now(UTC), "alice", "key.update", "k1", entry))
    latest = (await audit.recent(1))[0]
    assert (latest.operator, latest.action, latest.target, latest.detail) == (
        "alice",
        "key.update",
        "k1",
        entry,
    )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_audit WHERE target = 'k1'"))
    await engine.dispose()
