import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_models_lists_aliases_then_direct_models(auth: dict[str, str]) -> None:
    resp = client.get("/v1/models", headers=auth)
    assert resp.status_code == 200
    data = resp.json()["data"]
    aliases = [m["id"] for m in data if m["owned_by"] == "gateway"]
    assert aliases == list(config.registry.aliases)
    direct = {m["id"] for m in data if m["owned_by"] != "gateway"}
    assert direct == {e for a in config.registry.aliases.values() for e in a.chain}


def test_reload_rejected_without_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "")
    resp = client.post("/admin/reload", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "unauthorized"


def test_reload_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    resp = client.post("/admin/reload", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_reload_with_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    resp = client.post("/admin/reload", headers={"Authorization": "Bearer gw_admin_test"})
    assert resp.status_code == 200
    assert resp.json()["reloaded"] is True
