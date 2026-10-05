"""Policy routing: `model: auto` builds the chain per request (ADR 0017)."""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import config
from app.config import Alias, Catalog, CatalogEntry, Policy, Price, Pricing, Registry
from app.observability import live
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION

MSGS = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
IMAGE = [
    {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
    }
]


@pytest.fixture
def auto(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Candidates: mock/tiny (cheap, tools, q2), chaos/ok (mid, tools+vision, q4),
    claude/old (dear, tools+vision, q5, small context)."""
    live.reset()
    monkeypatch.setitem(
        registry.aliases,
        "auto",
        Alias(policy=Policy(candidates=["mock/tiny", "chaos/ok", "claude/old"], max_chain=3)),
    )
    monkeypatch.setattr(
        config,
        "pricing",
        Pricing(
            models={
                "mock/tiny": Price(input=0.1, output=0.5),
                "chaos/ok": Price(input=1, output=5),
                "claude/old": Price(input=3, output=15),
            }
        ),
    )
    monkeypatch.setattr(
        config,
        "catalog",
        Catalog(
            models={
                "mock/tiny": CatalogEntry(
                    context_window=100_000, capabilities=["tools"], quality=2
                ),
                "chaos/ok": CatalogEntry(
                    context_window=100_000, capabilities=["tools", "vision"], quality=4
                ),
                "claude/old": CatalogEntry(
                    context_window=2_000, capabilities=["tools", "vision"], quality=5
                ),
            }
        ),
    )
    return {"aliases": registry.aliases}


def chat(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": MSGS, **body},
        headers=headers or {},
    )


@respx.mock
def test_cost_policy_serves_the_cheapest_fit(client: TestClient, auto: Any) -> None:
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    resp = chat(client)
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "mock/tiny"
    assert resp.headers["x-gateway-route"] == "optimize=cost; considered=3; chain=3"


def test_quality_hint_reorders(client: TestClient, auto: Any) -> None:
    resp = chat(client, route={"optimize": "quality"})
    # claude/old has the best quality but isn't configured in this test (no client mock):
    # fallback walks the quality order, so chaos/ok (q4) serves.
    assert resp.status_code == 200
    assert resp.headers["x-gateway-route"].startswith("optimize=quality")
    # tried claude/old first (q5; unreachable here), then the next best
    assert resp.headers["x-gateway-provider"] == "chaos/ok"
    assert resp.headers["x-gateway-fallback"] == "true"


def test_header_hints(client: TestClient, auto: Any) -> None:
    resp = chat(client, headers={"x-gateway-route": "optimize=cost; needs=vision"})
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"


def test_implied_capabilities_filter_candidates(client: TestClient, auto: Any) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "auto", "messages": IMAGE})
    assert resp.headers["x-gateway-provider"] == "chaos/ok"  # mock/tiny has no vision


def test_context_window_filters_candidates(client: TestClient, auto: Any) -> None:
    resp = chat(client, route={"optimize": "quality"}, max_tokens=5_000)  # claude/old: 2K context
    assert resp.headers["x-gateway-provider"] == "chaos/ok"
    assert "chain=2" in resp.headers["x-gateway-route"]


def test_hints_only_tighten(client: TestClient, auto: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routing import policy

    base = Policy(min_quality=4, max_blended_price=10)
    tight = policy.effective(base, {"min_quality": 2, "max_blended_price": 50})
    assert tight.min_quality == 4 and tight.max_blended_price == 10  # can't loosen
    tighter = policy.effective(
        base, {"min_quality": 5, "max_blended_price": "2.5", "needs": ["tools"]}
    )
    assert (
        tighter.min_quality == 5 and tighter.max_blended_price == 2.5 and tighter.needs == ["tools"]
    )
    off = Policy(client_hints=False)
    assert policy.effective(off, {"optimize": "quality"}) is off


@pytest.mark.parametrize(
    "route",
    [
        {"optimize": "fastest-please"},
        {"candidates": ["x/y"]},
        {"needs": "tools"},
        {"min_quality": "high"},
    ],
)
def test_invalid_hints_are_a_400(client: TestClient, auto: Any, route: dict[str, Any]) -> None:
    resp = chat(client, route=route)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_route"


def test_nothing_fits_is_a_400(client: TestClient, auto: Any) -> None:
    resp = chat(client, route={"min_quality": 5, "needs": ["reasoning"]})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "no_route"


def test_nothing_available_is_a_503(
    client: TestClient, auto: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOCK_API_KEY")
    resp = chat(client, route={"max_blended_price": 0.3})  # only mock/tiny fits, and it has no key
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "no_route"


async def test_open_breakers_are_skipped(
    client: TestClient, auto: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routing import router
    from app.routing.breaker import State

    real = router.store.state

    async def state(target: str) -> State:
        return State.OPEN if target == "mock/tiny" else await real(target)

    monkeypatch.setattr(router.store, "state", state)
    resp = chat(client)
    assert resp.headers["x-gateway-provider"] == "chaos/ok"


def test_latency_policy_uses_measured_ttft(client: TestClient, auto: Any) -> None:
    live.record_served("chaos/ok", 1.0, 0.1)
    live.record_served("mock/tiny", 1.0, 0.9)
    resp = chat(client, route={"optimize": "latency"})
    assert resp.headers["x-gateway-provider"] == "chaos/ok"


def test_fallback_still_works_inside_a_policy_chain(client: TestClient, auto: Any) -> None:
    # mock/tiny has no respx mock here → network error → falls back to the next cheapest
    resp = chat(client)
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert resp.headers["x-gateway-fallback"] == "true"


def test_messages_api_passes_route_hints(client: TestClient, auto: Any) -> None:
    resp = client.post(
        "/v1/messages",
        json={"model": "auto", "max_tokens": 5, "messages": MSGS, "route": {"needs": ["vision"]}},
    )
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"


@respx.mock
def test_route_hints_never_reach_the_provider(client: TestClient, auto: Any) -> None:
    import json

    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    chat(client, route={"optimize": "cost"})
    assert "route" not in json.loads(route.calls.last.request.content)


def test_policy_aliases_are_listed(client: TestClient, auto: Any) -> None:
    models = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert models["auto"]["policy"]["optimize"] == "cost" and "chain" not in models["auto"]
    catalog = {a["id"]: a for a in client.get("/v1/catalog").json()["aliases"]}
    assert catalog["auto"]["candidates"] == ["mock/tiny", "chaos/ok", "claude/old"]


def test_an_alias_needs_exactly_one_of_chain_or_policy() -> None:
    with pytest.raises(ValueError):
        Alias()
    with pytest.raises(ValueError):
        Alias(chain=["a/b"], policy=Policy())


def test_shipped_auto_alias_excludes_test_providers() -> None:
    from pathlib import Path

    from app.config import load_registry
    from app.routing import policy

    reg = load_registry(Path(__file__).parent.parent / "config", enable_fake=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "registry", reg)
        pool = policy.candidates("auto", reg.aliases["auto"].policy)  # type: ignore[arg-type]
    assert pool and not any(t.startswith(("fake/", "bench/")) for t in pool)


def test_operators_can_forbid_optimize_hints(
    client: TestClient, auto: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_cfg = auto["aliases"]["auto"].policy
    monkeypatch.setattr(policy_cfg, "allowed_hints", ["needs"])
    resp = chat(client, route={"optimize": "quality"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_route"
    assert chat(client, route={"needs": ["tools"]}).status_code == 200


def test_routable_targets_include_policy_candidates(registry: Registry, auto: Any) -> None:
    assert {"mock/tiny", "chaos/ok", "claude/old"} <= registry.routable_targets()


@respx.mock
def test_route_hints_change_the_cache_key(
    client: TestClient, auto: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import cache, services
    from app.config import CacheConfig

    monkeypatch.setattr(services, "response_cache", cache.MemoryCacheStore())
    monkeypatch.setattr(auto["aliases"]["auto"], "cache", CacheConfig())
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    assert chat(client).headers["x-gateway-cache"] == "miss"
    assert chat(client).headers["x-gateway-cache"] == "hit"
    assert chat(client, route={"optimize": "quality"}).headers["x-gateway-cache"] == "miss"
