"""API keys, rate limits and budgets through the real endpoint (ADR 0006, 0007)."""

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main, services
from app.auth import CachedKeys, MemoryKeyStore
from tests.conftest import add_key

ADMIN = {"Authorization": "Bearer gw_admin_test"}
MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture
def anon(registry: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client with no key; admin key configured."""
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    return TestClient(main.app)


def chat(c: TestClient, key: str, model: str = "chaos/ok", **extra: Any) -> httpx.Response:
    return c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": MSGS, **extra},
    )


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize(
    "header", ["", "Bearer ", "Bearer gw_nope", "Bearer sk-not-ours", "Basic Z3c6eA=="]
)
def test_bad_or_missing_key_is_401_in_openai_shape(anon: TestClient, header: str) -> None:
    resp = anon.post(
        "/v1/chat/completions",
        headers={"Authorization": header} if header else {},
        json={"model": "chaos/ok", "messages": MSGS},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_models_endpoint_needs_a_key_and_is_filtered(anon: TestClient) -> None:
    assert anon.get("/v1/models").status_code == 401
    key = add_key(allowed_aliases=["local", "chaos/ok"])
    ids = [
        m["id"]
        for m in anon.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]
    ]
    assert ids == ["local", "chaos/ok"]


def test_healthz_stays_open(anon: TestClient) -> None:
    assert anon.get("/healthz").status_code == 200


def test_admin_creates_lists_and_revokes_keys(anon: TestClient) -> None:
    created = anon.post(
        "/admin/keys", headers=ADMIN, json={"name": "ci", "tier": "dev", "allowed_aliases": ["*"]}
    )
    assert created.status_code == 201
    key = created.json()["key"]
    assert key.startswith("gw_") and len(key) > 40
    assert chat(anon, key).status_code == 200

    listed = anon.get("/admin/keys", headers=ADMIN).json()["data"]
    assert key not in json.dumps(listed)  # never the key…
    assert all("hash" not in k for k in listed[0])  # …nor its hash
    assert any(k["prefix"] == key[:8] for k in listed)

    kid = created.json()["id"]
    assert anon.delete(f"/admin/keys/{kid}", headers=ADMIN).json()["revoked"] is True
    assert chat(anon, key).status_code == 401
    assert anon.delete(f"/admin/keys/{kid}", headers=ADMIN).status_code == 404


def test_admin_endpoints_need_the_admin_key(anon: TestClient) -> None:
    key = add_key()
    for method, path in [
        ("post", "/admin/keys"),
        ("get", "/admin/keys"),
        ("delete", "/admin/keys/x"),
        ("post", "/admin/reload"),
    ]:
        resp = getattr(anon, method)(path, headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 401, path  # a client key is not an admin key


def test_unknown_tier_is_rejected(anon: TestClient) -> None:
    resp = anon.post("/admin/keys", headers=ADMIN, json={"name": "x", "tier": "platinum"})
    assert resp.status_code == 400


def test_model_not_allowed_for_key_is_403(anon: TestClient) -> None:
    key = add_key(allowed_aliases=["local"])
    resp = chat(anon, key, "chaos/ok")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "model_not_allowed"


def test_revocation_reaches_other_instances_within_the_cache_ttl() -> None:
    import asyncio

    now = [0.0]
    store = MemoryKeyStore()
    key, plaintext = asyncio.run(store.create("x", "dev", {}))
    other_instance = CachedKeys(store, ttl=30, clock=lambda: now[0])
    assert asyncio.run(other_instance.authenticate(plaintext)) is not None
    asyncio.run(store.revoke(key.id))
    assert asyncio.run(other_instance.authenticate(plaintext)) is not None  # still cached
    now[0] += 31
    assert asyncio.run(other_instance.authenticate(plaintext)) is None


# --- rate limits -------------------------------------------------------------


def test_success_carries_ratelimit_headers(anon: TestClient) -> None:
    resp = chat(anon, add_key(requests_per_minute=5, allowed_aliases=["*"]))
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-limit-requests"] == "5"
    assert resp.headers["x-ratelimit-remaining-requests"] == "4"


def test_request_limit_429_with_retry_after(anon: TestClient) -> None:
    key = add_key(requests_per_minute=3, allowed_aliases=["*"])
    codes = [chat(anon, key).status_code for _ in range(3)]
    assert codes == [200, 200, 200]
    resp = chat(anon, key)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(resp.headers["retry-after"]) >= 1
    assert resp.headers["x-ratelimit-remaining-requests"] == "0"


def test_token_limit_counts_tokens_not_requests(anon: TestClient) -> None:
    key = add_key(tokens_per_minute=2000, allowed_aliases=["*"])
    big = [{"role": "user", "content": "x" * 6000}]  # ~1500 prompt tokens, really used
    ok = anon.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "chaos/ok", "messages": big, "max_tokens": 10},
    )
    assert ok.status_code == 200
    resp = anon.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "chaos/ok", "messages": big, "max_tokens": 10},
    )
    assert resp.status_code == 429  # 2nd request: plenty of request budget, no token budget
    assert resp.headers["x-ratelimit-remaining-requests"] != "0"


def test_estimate_is_reconciled_with_real_usage(anon: TestClient) -> None:
    """The fake provider uses ~5 tokens; the 1500 reserved up front is refunded after."""
    key = add_key(tokens_per_minute=2000, allowed_aliases=["*"])
    for _ in range(5):  # would need 7500 tokens without reconciliation
        assert chat(anon, key, max_tokens=1500).status_code == 200


def test_rate_limited_requests_dont_reach_the_provider(anon: TestClient) -> None:
    key = add_key(requests_per_minute=1, allowed_aliases=["*"])
    chat(anon, key)
    resp = chat(anon, key)
    assert resp.status_code == 429 and "x-gateway-attempts" not in resp.headers


# --- budgets -----------------------------------------------------------------


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    pricing = config.Pricing.model_validate({"models": {"chaos/ok": {"input": 1e6, "output": 1e6}}})
    monkeypatch.setattr(config, "pricing", pricing)  # $1 per token: easy maths


def test_budget_blocks_further_calls(anon: TestClient, priced: None) -> None:
    key = add_key(monthly_budget_usd=15, allowed_aliases=["*"])
    statuses = [chat(anon, key).status_code for _ in range(5)]
    # each call costs a few $ (prompt + "Hello from fake/ok." completion)
    assert statuses[0] == 200 and 429 in statuses
    blocked = chat(anon, key)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "insufficient_quota"


def test_spend_uses_the_target_that_served(anon: TestClient, priced: None) -> None:
    key = add_key(allowed_aliases=["*"], monthly_budget_usd=10**6)
    chat(anon, key, "down-then-ok")  # served by chaos/ok after the primary fails
    kid = next(
        k.id
        for k in services.keys.store._by_hash.values()  # type: ignore[attr-defined]
        if k.overrides.get("monthly_budget_usd") == 10**6
    )
    import asyncio

    assert asyncio.run(services.spend.spent(kid)) > 0


def test_failed_requests_cost_nothing(anon: TestClient, priced: None) -> None:
    key = add_key(allowed_aliases=["*"], tokens_per_minute=3000)
    for _ in range(3):
        assert chat(anon, key, "only-down", max_tokens=2000).status_code in (502, 503)
    # estimates were refunded: the bucket still has room
    assert chat(anon, key, max_tokens=2000).status_code == 200


# --- streaming usage ---------------------------------------------------------


def stream(c: TestClient, key: str, **extra: Any) -> list[dict[str, Any]]:
    with c.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "chaos/ok", "messages": MSGS, "stream": True, **extra},
    ) as r:
        return [json.loads(ln[6:]) for ln in r.iter_lines() if ln.startswith("data: {")]


def test_stream_usage_chunk_hidden_unless_asked(anon: TestClient, priced: None) -> None:
    key = add_key(allowed_aliases=["*"])
    chunks = stream(anon, key)
    assert not any("usage" in c for c in chunks)  # we asked upstream; the client didn't
    with_usage = stream(anon, key, stream_options={"include_usage": True})
    assert with_usage[-1]["usage"]["completion_tokens"] > 0


def test_streams_are_metered(anon: TestClient, priced: None) -> None:
    key = add_key(allowed_aliases=["*"], monthly_budget_usd=15)
    for _ in range(6):
        stream(anon, key)
    assert chat(anon, key).status_code == 429  # streams spent the budget
