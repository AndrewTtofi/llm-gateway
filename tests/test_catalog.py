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
            checked=dt.date(2026, 10, 1),
            models={
                "mock/tiny": Price(input=1.0, output=5.0, cached_input=0.1),
                "chaos/ok": Price(input=0.0, output=0.0),
                "claude/old": Price(input=3.0, output=15.0),
            },
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
    assert body["catalog_checked"] == "2026-10-05" and body["prices_checked"] == "2026-10-01"
    assert {"id": "local", "chain": ["mock/tiny"]} in body["aliases"]
    tiny = next(r for r in body["data"] if r["id"] == "mock/tiny")
    assert tiny["pricing"] == {
        "input": 1.0,
        "output": 5.0,
        "cached_input": 0.1,
        "cache_write": None,
        "cache_write_1h": None,
        "tiers": [],
        "off_peak": None,
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


# --- more endpoint behaviour ----------------------------------------------


def test_sort_by_live_speed_puts_unmeasured_last(client: TestClient, priced: None) -> None:
    live.record_served("claude/old", 2.0, 0.9)
    live.record_served("mock/tiny", 1.0, 0.3)
    assert ids(client.get("/v1/catalog?sort=ttft"))[:2] == ["mock/tiny", "claude/old"]
    assert ids(client.get("/v1/catalog?sort=latency"))[:2] == ["mock/tiny", "claude/old"]


def test_min_context_excludes_unknown_windows(client: TestClient, priced: None) -> None:
    assert "chaos/ok" not in ids(client.get("/v1/catalog?min_context=1"))


async def test_open_breaker_and_degraded_store_are_shown(
    client: TestClient, priced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routing import router
    from app.routing.breaker import State

    async def state(target: str) -> State:
        return State.OPEN if target == "mock/tiny" else State.CLOSED

    monkeypatch.setattr(router.store, "state", state)
    rows = {r["id"]: r for r in client.get("/v1/catalog").json()["data"]}
    assert rows["mock/tiny"]["circuit"] == "open" and rows["claude/old"]["circuit"] == "closed"
    monkeypatch.setattr(type(router.store), "degraded", True, raising=False)
    rows = {r["id"]: r for r in client.get("/v1/catalog").json()["data"]}
    assert rows["mock/tiny"]["circuit"] == "unknown"  # fail-open "closed" would be a guess


def test_live_window_reports_the_span_actually_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(live, "_clock", lambda: now[0])
    for _ in range(live.MAX_SAMPLES):
        now[0] += 0.01  # 100 rps: the cap fills in 20 s
        live.record_attempt("p/m", True)
    assert live.snapshot("p/m")["window_seconds"] == 19


def test_catalog_reloads_with_the_rest_of_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    for f in ("models.yaml", "pricing.yaml", "limits.yaml", "catalog.yaml"):
        shutil.copy(Path("config") / f, tmp_path / f)
    monkeypatch.setattr(config.settings, "config_dir", tmp_path)
    monkeypatch.setattr(config, "catalog", config.catalog)
    (tmp_path / "catalog.yaml").write_text("models:\n  x/y: { quality: 5 }\n")
    config.reload_registry()
    assert list(config.catalog.models) == ["x/y"]
    (tmp_path / "catalog.yaml").write_text("models:\n  x/y: { quality: 9 }\n")  # invalid
    with pytest.raises(ValueError):
        config.reload_registry()
    assert config.catalog.models["x/y"].quality == 5  # the old config stays live


def test_negative_or_non_finite_prices_are_rejected_on_load(tmp_path: Path) -> None:
    for bad in ("-1", ".nan", ".inf"):
        (tmp_path / "pricing.yaml").write_text(f"models:\n  a/b: {{ input: {bad}, output: 1 }}\n")
        with pytest.raises(ValueError):
            config.load_pricing(tmp_path)


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
    "llama3.2:3b": {
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "max_input_tokens": 128000,
    },
}
OPENROUTER = {
    "anthropic/claude-x-1": {
        "pricing": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"}
    },
    "openai/gpt-y": {"pricing": {"prompt": "0.000002", "completion": "0.000004"}},  # disagrees
}
MODELS_YAML = """
providers:
  anthropic: { type: anthropic }
  openai: { type: openai }
  ollama: { type: openai }
  fake: { type: fake }
  bench: { type: openai, dev_only: true }
aliases:
  smart: { chain: [anthropic/claude-x-1, openai/gpt-y, ollama/llama3.2:3b, fake/ok, bench/fast] }
"""
PRICING_YAML = """# header comment
checked: 2026-01-01
currency: USD
models:
  anthropic/claude-x-1:   { input: 2.00,  output: 15.00 }  # keep this comment
  openai/gpt-y:           { input: 1.00,  output: 4.00 }

# trailing comment block
"""
CATALOG_YAML = """# my header — keep me
checked: 2026-01-01
models:
  anthropic/claude-x-1: { context_window: 100000, capabilities: [vision, tools], quality: 4 }  # me
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "models.yaml").write_text(MODELS_YAML)
    (tmp_path / "pricing.yaml").write_text(PRICING_YAML)
    (tmp_path / "catalog.yaml").write_text(CATALOG_YAML)
    return tmp_path


def changes_by_key(changes: list[sync_prices.Change]) -> dict[tuple[str, str], sync_prices.Change]:
    return {(c.target, c.field): c for c in changes}


def test_sync_proposes_changes_with_a_status(config_dir: Path) -> None:
    changes, missing = sync_prices.diff(config_dir, LITELLM, OPENROUTER)
    found = changes_by_key(changes)
    assert found[("anthropic/claude-x-1", "input")].proposed == 3.0
    assert found[("anthropic/claude-x-1", "input")].status == "agreed"
    assert found[("anthropic/claude-x-1", "cached_input")].status == "agreed"
    assert found[("anthropic/claude-x-1", "context_window")].proposed == 200000
    assert ("anthropic/claude-x-1", "capabilities") not in found  # same set, other order
    assert ("openai/gpt-y", "input") not in found  # LiteLLM agrees with the current price
    # no OpenRouter entry for the local model: its new prices are unchecked
    assert found[("ollama/llama3.2:3b", "input")].status == "unchecked"
    assert missing == []  # fake/ and dev_only providers are never looked up


def test_sync_holds_back_disputed_and_unchecked_prices(config_dir: Path) -> None:
    litellm = {
        **LITELLM,
        "gpt-y": {"input_cost_per_token": 1.5e-06, "output_cost_per_token": 4e-06},
    }
    found = changes_by_key(sync_prices.diff(config_dir, litellm, OPENROUTER)[0])
    disputed = found[("openai/gpt-y", "input")]
    assert disputed.status == "disputed" and disputed.check == 2.0 and not disputed.safe
    assert not found[("ollama/llama3.2:3b", "input")].safe
    assert found[("ollama/llama3.2:3b", "context_window")].safe  # facts don't bill anyone


@pytest.mark.parametrize("bad", ["NaN", "inf", -1e-06, "abc", True, None, [1]])
def test_sync_ignores_invalid_remote_prices(config_dir: Path, bad: Any) -> None:
    litellm = {"claude-x-1": {"input_cost_per_token": bad, "output_cost_per_token": bad}}
    changes, _ = sync_prices.diff(config_dir, litellm, {})
    assert not [c for c in changes if c.is_price]


@pytest.mark.parametrize("bad", ["5000000, capabilities: [vision]", "1\nx: 2", -5, 1.5, True])
def test_sync_ignores_invalid_remote_sizes(config_dir: Path, bad: Any) -> None:
    litellm = {"claude-x-1": {"max_input_tokens": bad, "max_output_tokens": bad}}
    changes, _ = sync_prices.diff(config_dir, litellm, {})
    assert not changes


def test_sync_write_applies_safe_changes_in_place(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    litellm = {
        **LITELLM,
        "gpt-y": {"input_cost_per_token": 1.5e-06, "output_cost_per_token": 4e-06},
    }
    monkeypatch.setattr(sync_prices, "fetch", lambda: (litellm, OPENROUTER))
    assert sync_prices.main(["--config-dir", str(config_dir), "--write"]) == 1
    today = dt.date.today().isoformat()

    pricing = (config_dir / "pricing.yaml").read_text()
    assert (
        "  anthropic/claude-x-1:   { input: 3.00, output: 15.00, cached_input: 0.30 }"
        "  # keep this comment" in pricing
    )
    assert "openai/gpt-y:           { input: 1.00,  output: 4.00 }" in pricing  # disputed: kept
    assert "ollama/llama3.2:3b" not in pricing  # unchecked: not added without --force
    assert f"checked: {today}" in pricing and "# trailing comment block" in pricing

    catalog_text = (config_dir / "catalog.yaml").read_text()
    assert "# my header — keep me" in catalog_text and "# me" in catalog_text
    catalog = config.load_catalog(config_dir)
    entry = catalog.models["anthropic/claude-x-1"]
    assert entry.quality == 4 and entry.context_window == 200000  # quality untouched
    assert catalog.models["ollama/llama3.2:3b"].context_window == 128000  # a new line
    assert catalog.checked == dt.date.today()


def test_sync_force_adds_a_new_target_with_regex_special_characters(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_prices, "fetch", lambda: (LITELLM, OPENROUTER))
    sync_prices.main(["--config-dir", str(config_dir), "--write", "--force"])
    loaded = config.load_pricing(config_dir)
    assert loaded.models["ollama/llama3.2:3b"].input == 0.0
    assert loaded.models["anthropic/claude-x-1"].input == 3.0  # not clobbered
    text = (config_dir / "pricing.yaml").read_text()
    # inserted inside the models block, before the trailing comment
    assert text.index("ollama/llama3.2:3b") < text.index("# trailing comment block")


def test_sync_doesnt_touch_files_when_nothing_is_applied(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    litellm = {"gpt-y": {"input_cost_per_token": 1.5e-06, "output_cost_per_token": 4e-06}}
    monkeypatch.setattr(sync_prices, "fetch", lambda: (litellm, OPENROUTER))
    assert sync_prices.main(["--config-dir", str(config_dir), "--write"]) == 1  # disputed only
    assert (config_dir / "pricing.yaml").read_text() == PRICING_YAML
    assert (config_dir / "catalog.yaml").read_text() == CATALOG_YAML


def test_sync_reports_up_to_date(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(sync_prices, "fetch", lambda: ({}, {}))
    assert sync_prices.main(["--config-dir", str(config_dir), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changes"] == []
    assert set(out["no_source"]) == {"anthropic/claude-x-1", "openai/gpt-y", "ollama/llama3.2:3b"}


def test_sync_target_found_by_full_id_is_not_reported_missing(config_dir: Path) -> None:
    litellm = {"openai/gpt-y": {"input_cost_per_token": 1e-06, "output_cost_per_token": 4e-06}}
    _, missing = sync_prices.diff(config_dir, litellm, {})
    assert "openai/gpt-y" not in missing


def test_sync_exit_codes(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def offline() -> Any:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(sync_prices, "fetch", offline)
    assert sync_prices.main(["--config-dir", str(config_dir)]) == sync_prices.EXIT_FETCH
    monkeypatch.setattr(sync_prices, "fetch", lambda: (LITELLM, OPENROUTER))
    (config_dir / "models.yaml").write_text("providers: [oops")  # broken config: not drift
    assert sync_prices.main(["--config-dir", str(config_dir)]) == sync_prices.EXIT_INTERNAL


def test_written_strings_are_valid_yaml() -> None:
    import yaml

    for value in ["plain", "yes", "1.5", "a: b", "x\ny", "{oops}"]:
        line = sync_prices._flow({"k": value})
        assert yaml.safe_load(f"v: {line}")["v"]["k"] == value


def test_openrouter_id_guess() -> None:
    assert (
        sync_prices.openrouter_id("anthropic/claude-haiku-4-5-20251001")
        == "anthropic/claude-haiku-4.5"
    )
    assert sync_prices.openrouter_id("openai/gpt-6.1-sol") == "openai/gpt-6.1-sol"


def test_catalog_marks_providers_without_a_key(
    client: TestClient, priced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {r["id"]: r for r in client.get("/v1/catalog").json()["data"]}
    assert rows["mock/tiny"]["configured"] is True
    assert rows["chaos/ok"]["configured"] is True  # no key needed
    from app import providers
    from app.providers import AdapterPool

    monkeypatch.delenv("MOCK_API_KEY")
    monkeypatch.setattr(providers, "pool", AdapterPool())  # keys are read when adapters start
    rows = {r["id"]: r for r in client.get("/v1/catalog").json()["data"]}
    assert rows["mock/tiny"]["configured"] is False


def test_catalog_hides_capabilities_the_gateway_cannot_use(
    client: TestClient, priced: None, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.providers["mock"], "models", {"tiny": {"tools": False}})
    tiny = next(r for r in client.get("/v1/catalog").json()["data"] if r["id"] == "mock/tiny")
    assert "tools" not in tiny["capabilities"]  # the model has it; this API path doesn't


def test_sync_leaves_pinned_facts_alone(config_dir: Path) -> None:
    (config_dir / "catalog.yaml").write_text(
        "models:\n  anthropic/claude-x-1: { capabilities: [tools], pinned: [capabilities] }\n"
    )
    found = changes_by_key(sync_prices.diff(config_dir, LITELLM, OPENROUTER)[0])
    assert ("anthropic/claude-x-1", "capabilities") not in found
    assert ("anthropic/claude-x-1", "context_window") in found  # not pinned


# --- cost accuracy (ADR 0015) -------------------------------------------------


def test_long_context_tier_reprices_the_whole_request() -> None:
    from app.config import PriceTier

    p = Pricing(
        models={
            "a/b": Price(
                input=10,
                output=50,
                tiers=[PriceTier(above_prompt_tokens=272_000, input=20, output=75)],
            )
        }
    )
    assert p.cost("a/b", 272_000, 1000) == pytest.approx((272_000 * 10 + 1000 * 50) / 1e6)
    assert p.cost("a/b", 272_001, 1000) == pytest.approx((272_001 * 20 + 1000 * 75) / 1e6)


def test_cache_writes_are_billed_at_their_own_price() -> None:
    p = Pricing(
        models={
            "a/b": Price(input=2, output=10, cached_input=0.2, cache_write=2.5, cache_write_1h=4)
        }
    )
    # 1M prompt: 100K read, 300K written (100K of them to the 1-hour cache), 600K plain
    usd = p.cost(
        "a/b",
        1_000_000,
        0,
        cached_tokens=100_000,
        written_tokens=300_000,
        written_1h_tokens=100_000,
    )
    assert usd == pytest.approx((600_000 * 2 + 100_000 * 0.2 + 200_000 * 2.5 + 100_000 * 4) / 1e6)
    no_write_price = Pricing(models={"a/b": Price(input=2, output=10)})
    assert no_write_price.cost("a/b", 1000, 0, written_tokens=1000) == pytest.approx(0.002)


def test_off_peak_windows() -> None:
    from app.config import OffPeak

    off = OffPeak(multiplier=0.5, peak_utc=["01:00-04:00", "06:00-10:00"])
    p = Pricing(models={"a/b": Price(input=1, output=1, off_peak=off)})
    monday = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)
    assert p.cost("a/b", 1_000_000, 0, at=monday.replace(hour=2)) == pytest.approx(1.0)
    assert p.cost("a/b", 1_000_000, 0, at=monday.replace(hour=4)) == pytest.approx(
        0.5
    )  # end exclusive
    assert p.cost("a/b", 1_000_000, 0, at=monday.replace(hour=12)) == pytest.approx(0.5)
    saturday = dt.datetime(2026, 10, 3, 2, tzinfo=dt.UTC)
    assert p.cost("a/b", 1_000_000, 0, at=saturday) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        OffPeak(multiplier=0.5, peak_utc=["25:00-26:00"])


def test_tiers_must_ascend() -> None:
    from app.config import PriceTier

    with pytest.raises(ValueError):
        Price(
            input=1,
            output=1,
            tiers=[
                PriceTier(above_prompt_tokens=2, input=1, output=1),
                PriceTier(above_prompt_tokens=1, input=1, output=1),
            ],
        )


def test_meter_bills_anthropic_cache_writes(registry: Registry) -> None:
    from app.metering import Meter
    from app.providers.anthropic_format import usage_to_openai

    usage = usage_to_openai(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 500,
            "cache_creation": {"ephemeral_1h_input_tokens": 200},
        }
    )
    assert usage["prompt_tokens"] == 1600
    assert usage["prompt_tokens_details"] == {
        "cached_tokens": 1000,
        "cache_creation_tokens": 500,
        "cache_creation_1h_tokens": 200,
    }
    m = Meter.__new__(Meter)
    m.usage, m.target, m.prompt_estimate, m._chars = usage, "claude/old", 0, 0
    m.started_at = dt.datetime.now(dt.UTC)
    old = Pricing(
        models={
            "claude/old": Price(
                input=3, output=15, cached_input=0.3, cache_write=3.75, cache_write_1h=6
            )
        }
    )
    import app.config as cfg

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cfg, "pricing", old)
        used = m._actual()
    assert used.usd == pytest.approx((100 * 3 + 1000 * 0.3 + 300 * 3.75 + 200 * 6 + 10 * 15) / 1e6)


def test_sync_proposes_tiers_and_cache_writes(config_dir: Path) -> None:
    litellm = {
        "gpt-y": {
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 4e-06,
            "cache_creation_input_token_cost": 1.25e-06,
            "input_cost_per_token_above_272k_tokens": 2e-06,
            "output_cost_per_token_above_272k_tokens": 6e-06,
            "input_cost_per_token_above_272k_tokens_priority": 9e-06,  # other tiers: ignored
        }
    }
    openrouter = {
        "openai/gpt-y": {
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000004",
                "input_cache_write": "0.00000125",
                "overrides": [
                    {"min_prompt_tokens": 272000, "prompt": "0.000002", "completion": "0.000006"}
                ],
            }
        }
    }
    found = changes_by_key(sync_prices.diff(config_dir, litellm, openrouter)[0])
    assert found[("openai/gpt-y", "cache_write")].status == "agreed"
    tiers = found[("openai/gpt-y", "tiers")]
    assert tiers.status == "agreed"
    assert tiers.proposed == [{"above_prompt_tokens": 272000, "input": 2.0, "output": 6.0}]


def test_sync_writes_tiers_as_valid_yaml(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    litellm = {
        "gpt-y": {
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 4e-06,
            "input_cost_per_token_above_272k_tokens": 2e-06,
            "output_cost_per_token_above_272k_tokens": 6e-06,
        }
    }
    openrouter = {
        "openai/gpt-y": {
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000004",
                "overrides": [
                    {"min_prompt_tokens": 272000, "prompt": "0.000002", "completion": "0.000006"}
                ],
            }
        }
    }
    monkeypatch.setattr(sync_prices, "fetch", lambda: (litellm, openrouter))
    sync_prices.main(["--config-dir", str(config_dir), "--write"])
    price = config.load_pricing(config_dir).models["openai/gpt-y"]
    assert price.tiers[0].above_prompt_tokens == 272000 and price.tiers[0].input == 2.0
    assert (
        "above_prompt_tokens: 272000," in (config_dir / "pricing.yaml").read_text()
    )  # not 272000.00


def test_bare_model_ids_from_other_providers_are_ignored(config_dir: Path) -> None:
    litellm = {
        "gpt-y": {
            "input_cost_per_token": 9e-06,
            "output_cost_per_token": 9e-06,
            "litellm_provider": "vertex_ai",
        }
    }
    _, missing = sync_prices.diff(config_dir, litellm, {})
    assert "openai/gpt-y" in missing


@pytest.mark.parametrize("window", ["22:00-02:00", "10:00-10:00", "24:59-25:00", "09:00-24:30"])
def test_invalid_off_peak_windows_are_rejected(window: str) -> None:
    from app.config import OffPeak

    with pytest.raises(ValueError):
        OffPeak(multiplier=0.5, peak_utc=[window])


def test_off_peak_end_of_day_window() -> None:
    from app.config import OffPeak

    off = OffPeak(multiplier=0.5, peak_utc=["22:00-24:00"])
    assert off.is_peak(dt.datetime(2026, 10, 5, 23, 59, tzinfo=dt.UTC))
