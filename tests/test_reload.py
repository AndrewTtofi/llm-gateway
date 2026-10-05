"""Swapping a model is a YAML edit + reload — no restart, no code change."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, main

ADMIN = {"Authorization": "Bearer gw_admin_test"}


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree("config", tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(config.settings, "config_dir", tmp_path)
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    monkeypatch.setattr(config, "registry", config.load_registry(tmp_path))
    return tmp_path


def chain(client: TestClient, alias: str) -> list[str]:
    models = client.get("/v1/models").json()["data"]
    return next(m["chain"] for m in models if m["id"] == alias)


def test_yaml_edit_plus_reload_swaps_the_model(cfg_dir: Path) -> None:
    client = TestClient(main.app)
    before = chain(client, "local")
    models_yaml = cfg_dir / "models.yaml"
    models_yaml.write_text(
        models_yaml.read_text().replace(
            "  local:\n    chain:\n      - ollama/llama3.2:3b",
            "  local:\n    chain:\n      - ollama/swapped:1b",
        )
    )
    assert chain(client, "local") == before  # nothing changes until reload

    resp = client.post("/admin/reload", headers=ADMIN)
    assert resp.status_code == 200 and resp.json()["reloaded"] is True
    assert chain(client, "local") == ["ollama/swapped:1b"]
    _, upstream_model, target = main.resolve_target("local")
    assert (upstream_model, target) == ("swapped:1b", "ollama/swapped:1b")


def test_broken_yaml_keeps_the_previous_config(cfg_dir: Path) -> None:
    client = TestClient(main.app)
    before = chain(client, "local")
    (cfg_dir / "models.yaml").write_text("aliases: [this is: not valid")
    resp = client.post("/admin/reload", headers=ADMIN)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_config"
    assert chain(client, "local") == before


def test_sighup_handler_reloads_and_survives_errors(cfg_dir: Path) -> None:
    (cfg_dir / "models.yaml").write_text(
        (cfg_dir / "models.yaml").read_text().replace("ollama/llama3.2:3b", "ollama/hup:1b")
    )
    main.reload_from_signal()
    assert config.registry.resolve("local") == ["ollama/hup:1b"]

    (cfg_dir / "models.yaml").write_text("{{{")
    main.reload_from_signal()  # logs, doesn't raise
    assert config.registry.resolve("local") == ["ollama/hup:1b"]


def test_provider_config_change_gets_a_fresh_adapter(cfg_dir: Path) -> None:
    pool = main.pool
    a1, _, _ = main.resolve_target("local")
    assert main.resolve_target("local")[0] is a1  # reused while config is unchanged
    (cfg_dir / "models.yaml").write_text(
        (cfg_dir / "models.yaml")
        .read_text()
        .replace("connect: 2, first_token: 60", "connect: 3, first_token: 60")
    )
    config.reload_registry()
    a2, _, _ = main.resolve_target("local")
    assert a2 is not a1 and pool is main.pool
