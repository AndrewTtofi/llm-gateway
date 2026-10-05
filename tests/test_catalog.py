"""GET /v1/catalog, live per-target stats, and the price sync tool (ADR 0011)."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import config
from app.config import Catalog, CatalogEntry, Price, Pricing, Registry
from app.observability import live
from tests.conftest import UPSTREAM, add_key
from tests.test_chat import COMPLETION
from tools import sync_prices

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def fresh_stats() -> None:
    live.reset()


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> None:
    monkeypatch.setattr(
        config,
        "pricing",
        Pricing(
            models={
                "mock/tiny": Price(input=1.0, output=5.0, cached_input=0.1),
                "chaos/ok": Price(input=0.0, output=0.0),
                "claude/old": Price(input=3.0, output=15.0),
            }
        ),
    )
    monkeypatch.setattr(
        config,
        "catalog",
        Catalog(
            checked=dt.date(2026, 10, 5),
            models={
                "mock/tiny": CatalogEntry(context_window=8000, capabilities=["tools"], quality=2),
                "claude/old": CatalogEntry(
                    context_window=200000, capabilities=["tools", "vision"], quality=4
                ),
            },
        ),
    )


def ids(resp: Any) -> list[str]:
    return [r["id"] for r in resp.json()["data"]]


def test_catalog_lists_prices_facts_and_aliases(client: TestClient, priced: None) -> None:
    resp = client.get("/v1/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prices_checked"] == "2026-10-05"
    assert {"id": "local", "chain": ["mock/tiny"]} in body["aliases"]
    tiny = next(r for r in body["data"] if r["id"] == "mock/tiny")
    assert tiny["pricing"] == {
        "input": 1.0,
        "output": 5.0,
        "cached_input": 0.1,
        "blended": 2.0,  # (3 × 1 + 1 × 5) / 4
        "unit": "USD per 1M tokens",
    }
    assert tiny["context_window"] == 8000 and tiny["capabilities"] == ["tools"]
    assert tiny["quality"] == 2 and tiny["circuit"] == "closed"
    assert "local" in tiny["in_aliases"] and tiny["callable_directly"] is True
    assert tiny["live"]["requests"] == 0 and tiny["live"]["latency_ms"] is None
    unpriced = next(r for r in body["data"] if r["id"] == "chaos/down")
    assert unpriced["pricing"] is None and unpriced["quality"] is None


def test_catalog_filters_and_sorts(client: TestClient, priced: None) -> None:
    assert ids(client.get("/v1/catalog?capability=vision")) == ["claude/old"]
    assert ids(client.get("/v1/catalog?capability=tools&min_context=10000")) == ["claude/old"]
    by_price = ids(client.get("/v1/catalog?sort=price"))
    assert by_price[:3] == ["chaos/ok", "mock/tiny", "claude/old"]  # unpriced ones last
    assert ids(client.get("/v1/catalog?sort=quality"))[:2] == ["claude/old", "mock/tiny"]
    bad = client.get("/v1/catalog?capability=telepathy")
    assert bad.status_code == 400 and bad.json()["error"]["param"].startswith("query")


def test_catalog_only_shows_what_the_key_may_use(registry: Registry, priced: None) -> None:
    from app import main

    key = add_key("dev")  # the test dev tier allows fast + local only
    with TestClient(main.app, headers={"Authorization": f"Bearer {key}"}) as c:
        resp = c.get("/v1/catalog")
    allowed = config.limits.tiers["dev"].allowed_aliases
    assert {a["id"] for a in resp.json()["aliases"]} <= set(allowed)
    assert "claude/old" not in ids(resp)


def test_catalog_needs_a_key_and_is_rate_limited(registry: Registry, priced: None) -> None:
    from app import main

    with TestClient(main.app) as c:
        assert c.get("/v1/catalog").status_code == 401
    key = add_key(requests_per_minute=1)
    with TestClient(main.app, headers={"Authorization": f"Bearer {key}"}) as c:
        assert c.get("/v1/catalog").status_code == 200
        assert c.get("/v1/catalog").status_code == 429


@respx.mock
def test_live_stats_follow_real_traffic(client: TestClient, priced: None) -> None:
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    # chaos/down fails twice (retry), then chaos/ok serves
    client.post("/v1/chat/completions", json={"model": "down-then-ok", "messages": MSGS})
    rows = {r["id"]: r for r in client.get("/v1/catalog").json()["data"]}
    assert rows["mock/tiny"]["live"]["requests"] == 1
    assert rows["mock/tiny"]["live"]["error_rate"] == 0
    assert rows["mock/tiny"]["live"]["latency_ms"]["p50"] >= 0
    assert rows["chaos/down"]["live"]["error_rate"] == 1.0
    assert rows["chaos/down"]["live"]["requests"] == 0  # never served
    assert rows["chaos/ok"]["live"]["requests"] == 1


def test_live_window_and_memory_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(live, "_clock", lambda: now[0])
    live.record_served("p/m", 0.2, 0.1)
    live.record_attempt("p/m", True)
    live.record_attempt("p/m", False)
    snap = live.snapshot("p/m")
    assert snap["requests"] == 1 and snap["error_rate"] == 0.5
    assert snap["ttft_ms"] == {"p50": 100.0, "p95": 100.0}
    now[0] += live.WINDOW_SECONDS + 1
    assert live.snapshot("p/m")["requests"] == 0  # aged out
    for _ in range(live.MAX_SAMPLES + 50):
        live.record_attempt("p/m", True)
    assert len(live._targets["p/m"].attempts) == live.MAX_SAMPLES


# --- price sync -------------------------------------------------------------

LITELLM = {
    "claude-x-1": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "max_input_tokens": 200000,
        "max_output_tokens": 64000,
        "supports_function_calling": True,
        "supports_vision": True,
    },
    "gpt-y": {"input_cost_per_token": 1e-06, "output_cost_per_token": 4e-06},
}
OPENROUTER = {
    "anthropic/claude-x.1": {"pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    "openai/gpt-y": {"pricing": {"prompt": "0.000002", "completion": "0.000004"}},  # disagrees
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "models.yaml").write_text(
        """
providers:
  anthropic: { type: anthropic }
  openai: { type: openai }
  fake: { type: fake }
  bench: { type: openai, dev_only: true }
aliases:
  smart: { chain: [anthropic/claude-x-1, openai/gpt-y, fake/ok, bench/fast] }
"""
    )
    (tmp_path / "pricing.yaml").write_text(
        """# header comment
# Last checked: 2026-01-01
currency: USD
models:
  anthropic/claude-x-1:   { input: 2.00,  output: 15.00 }  # keep this comment
  openai/gpt-y:           { input: 1.00,  output: 4.00 }
"""
    )
    (tmp_path / "catalog.yaml").write_text(
        "models:\n  anthropic/claude-x-1: { context_window: 100000, quality: 4 }\n"
    )
    return tmp_path


def test_sync_proposes_changes_and_flags_disagreement(config_dir: Path) -> None:
    changes, missing = sync_prices.diff(config_dir, LITELLM, OPENROUTER)
    found = {(c.target, c.field): c for c in changes}
    assert found[("anthropic/claude-x-1", "input")].proposed == 3.0
    assert found[("anthropic/claude-x-1", "input")].agreed  # both sources say 3.00
    assert found[("anthropic/claude-x-1", "cached_input")].proposed == 0.3
    assert found[("anthropic/claude-x-1", "context_window")].proposed == 200000
    assert found[("anthropic/claude-x-1", "capabilities")].proposed == ["tools", "vision"]
    assert ("openai/gpt-y", "input") not in found  # LiteLLM agrees with the current price
    assert missing == []  # fake/ and dev_only providers are never looked up


def test_sync_holds_back_disputed_prices(config_dir: Path) -> None:
    litellm = {
        **LITELLM,
        "gpt-y": {"input_cost_per_token": 1.5e-06, "output_cost_per_token": 4e-06},
    }
    changes, _ = sync_prices.diff(config_dir, litellm, OPENROUTER)
    disputed = next(c for c in changes if c.target == "openai/gpt-y" and c.field == "input")
    assert not disputed.agreed and disputed.check == 2.0


def test_sync_write_keeps_comments_alignment_and_quality(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    litellm = {
        **LITELLM,
        "gpt-y": {"input_cost_per_token": 1.5e-06, "output_cost_per_token": 4e-06},
    }
    monkeypatch.setattr(sync_prices, "fetch", lambda: (litellm, OPENROUTER))
    assert sync_prices.main(["--config-dir", str(config_dir), "--write"]) == 1
    pricing = (config_dir / "pricing.yaml").read_text()
    assert (
        "  anthropic/claude-x-1:   { input: 3.00, output: 15.00, cached_input: 0.30 }"
        "  # keep this comment" in pricing
    )
    assert "openai/gpt-y:           { input: 1.00,  output: 4.00 }" in pricing  # disputed: kept
    assert f"# Last checked: {dt.date.today().isoformat()}" in pricing
    catalog = config.load_catalog(config_dir)
    entry = catalog.models["anthropic/claude-x-1"]
    assert entry.quality == 4 and entry.context_window == 200000  # quality untouched
    assert config.load_pricing(config_dir).models["anthropic/claude-x-1"].input == 3.0


def test_sync_reports_up_to_date(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(sync_prices, "fetch", lambda: ({}, {}))
    assert sync_prices.main(["--config-dir", str(config_dir), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changes"] == [] and set(out["no_source"]) == {
        "anthropic/claude-x-1",
        "openai/gpt-y",
    }


def test_openrouter_id_guess() -> None:
    assert (
        sync_prices.openrouter_id("anthropic/claude-haiku-4-5-20251001")
        == "anthropic/claude-haiku-4.5"
    )
    assert sync_prices.openrouter_id("openai/gpt-6.1-sol") == "openai/gpt-6.1-sol"


def test_sync_fetch_failure_is_not_drift(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> Any:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(sync_prices, "fetch", down)
    assert sync_prices.main(["--config-dir", str(config_dir)]) == 2
