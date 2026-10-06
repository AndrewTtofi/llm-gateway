"""Regression tests for the Phase 4 code review."""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main, services
from app.auth import ApiKey, CachedKeys, MemoryKeyStore, generate_key, hash_key
from app.metering import Meter
from app.ratelimit import MemoryLimiter, MemorySpend
from tests.conftest import add_key

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    pricing = config.Pricing.model_validate(
        {
            "models": {
                "chaos/ok": {"input": 1e6, "output": 1e6},
                "chaos/slow": {"input": 1e6, "output": 1e6},
                "claude/old": {"input": 1e6, "output": 1e6},
                "claude/cheap": {"input": 1, "output": 1},
            }
        }
    )
    monkeypatch.setattr(config, "pricing", pricing)


def kid_of(plaintext: str) -> str:
    store = services.keys.store
    assert isinstance(store, MemoryKeyStore)
    return store._by_hash[hash_key(plaintext)].id


# --- budgets: reservation and disconnects -------------------------------------


@pytest.mark.usefixtures("registry", "priced")
async def test_concurrent_requests_cant_all_slip_under_the_budget(auth: dict[str, str]) -> None:
    """Review: spend was only added at settle, so slow requests all saw the old figure."""
    key = add_key(allowed_aliases=["*"], monthly_budget_usd=2500)  # ~2 calls' estimates
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gw", headers={"Authorization": f"Bearer {key}"}
    ) as gw:
        responses = await asyncio.gather(
            *(
                gw.post(
                    "/v1/chat/completions",
                    json={"model": "chaos/slow", "messages": MSGS, "max_tokens": 1000},
                )
                for _ in range(6)
            )
        )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) <= 3 and 429 in statuses  # reservations held the line


@pytest.mark.usefixtures("registry", "priced")
async def test_hanging_up_is_not_free() -> None:
    """Review: a client disconnecting before the answer paid nothing."""
    from tests.test_disconnect import call_app

    key = add_key(allowed_aliases=["*"])
    hang_up = asyncio.Event()
    task = asyncio.create_task(
        call_app(
            {"model": "chaos/slow", "messages": [{"role": "user", "content": "x" * 400}]},
            hang_up,
            key=key,
        )
    )
    await asyncio.sleep(0.1)  # the provider is "generating"
    hang_up.set()
    await task
    assert await services.spend.spent(kid_of(key)) >= 100  # ~100 prompt tokens at $1 each


# --- metering details ---------------------------------------------------------


async def meter_for(usage: dict[str, Any] | None, target: str, chars: int = 0) -> float:
    spend, key = MemorySpend(), ApiKey(id="k", name="n", prefix="p", tier="dev")
    m = Meter(key, key.limits(config.limits), MemoryLimiter(), spend, 100, 50, False)
    m.target, m.usage, m._chars = target, usage, chars
    await m.settle()
    return await spend.spent("k")


@pytest.mark.usefixtures("priced")
async def test_refusal_fallback_attempts_are_all_billed_at_their_own_rates() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "iterations": [
            {"type": "message", "model": "old", "input_tokens": 10, "output_tokens": 3},
            {"type": "fallback_message", "model": "cheap", "input_tokens": 10, "output_tokens": 5},
        ],
    }
    usd = await meter_for(usage, "claude/old")
    assert usd == pytest.approx(13 + 15e-6)  # declined attempt at "old", rescue at "cheap"


@pytest.mark.usefixtures("priced")
async def test_usage_less_answers_still_count_their_output() -> None:
    assert await meter_for(None, "chaos/ok", chars=400) == 50 + 100  # prompt est + 400/4


def test_client_stream_options_survive_forced_usage(client: TestClient) -> None:
    seen: dict[str, Any] = {}
    adapter, _, _ = main.resolve_target("chaos/ok")
    original = adapter.stream

    def spy(model: str, request: dict[str, Any]) -> Any:
        seen.update(request)
        return original(model, request)

    adapter.stream = spy  # type: ignore[method-assign]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "chaos/ok",
            "messages": MSGS,
            "stream": True,
            "stream_options": {"include_obfuscation": False},
        },
    ) as r:
        list(r.iter_lines())
    assert seen["stream_options"] == {"include_obfuscation": False, "include_usage": True}


@pytest.mark.parametrize(
    "bad", [{"max_tokens": "abc"}, {"max_tokens": -5}, {"max_completion_tokens": 0}]
)
def test_invalid_max_tokens_is_400(client: TestClient, bad: dict[str, Any]) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS, **bad})
    assert resp.status_code == 400


def test_accounting_failure_doesnt_break_a_delivered_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Meter) -> None:
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(Meter, "settle", boom)
    assert (
        client.post(
            "/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS}
        ).status_code
        == 200
    )


# --- key lookup ---------------------------------------------------------------


class CountingStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0
        self.down = False

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        self.lookups += 1
        await asyncio.sleep(0.01)
        if self.down:
            raise ConnectionError("postgres down")
        return await super().get_by_hash(key_hash)


async def test_malformed_keys_never_reach_the_store() -> None:
    store = CountingStore()
    keys = CachedKeys(store)
    for junk in ["gw_short", "gw_" + "x" * 100, "gw_" + "!" * 43, "sk-abc", ""]:
        assert await keys.authenticate(junk) is None
    assert store.lookups == 0


async def test_a_flood_of_unknown_keys_doesnt_evict_valid_ones() -> None:
    store = CountingStore()
    _, valid = await store.create("v", "dev", {})
    keys = CachedKeys(store, max_entries=50)
    assert await keys.authenticate(valid) is not None
    for _ in range(200):
        await keys.authenticate(generate_key())
    before = store.lookups
    assert await keys.authenticate(valid) is not None
    assert store.lookups == before  # still cached


async def test_concurrent_lookups_of_one_key_share_one_query() -> None:
    store = CountingStore()
    _, valid = await store.create("v", "dev", {})
    keys = CachedKeys(store)
    results = await asyncio.gather(*(keys.authenticate(valid) for _ in range(20)))
    assert all(r is not None for r in results) and store.lookups == 1


async def test_store_outage_serves_recent_keys_and_rejects_unknown() -> None:
    now = [0.0]
    store = CountingStore()
    _, valid = await store.create("v", "dev", {})
    keys = CachedKeys(store, ttl=30, stale_ttl=600, clock=lambda: now[0])
    assert await keys.authenticate(valid) is not None
    store.down = True
    now[0] += 60  # past ttl, within stale_ttl
    assert await keys.authenticate(valid) is not None  # stale-if-error
    with pytest.raises(ConnectionError):
        await keys.authenticate(generate_key())  # unknown + store down → fail closed
    now[0] += 600
    with pytest.raises(ConnectionError):
        await keys.authenticate(valid)  # too stale


# --- admin ----------------------------------------------------------------------


def test_non_ascii_admin_header_is_401_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    c = TestClient(main.app)
    resp = c.get("/admin/keys", headers=[(b"authorization", b"Bearer \xff\xfe")])
    assert resp.status_code == 401


def test_typo_in_new_key_fields_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    c = TestClient(main.app)
    resp = c.post(
        "/admin/keys",
        headers={"Authorization": "Bearer gw_admin_test"},
        json={"name": "x", "tier": "dev", "monthly_budget": 5},
    )
    assert resp.status_code == 400


class SlowSpend(MemorySpend):
    """Spend with Redis-like latency on every call. Reads and adds yield to other
    requests; only `reserve` is atomic, as the Lua script is in Redis."""

    async def spent(self, key_id: str) -> float:
        await asyncio.sleep(0.02)
        return await super().spent(key_id)

    async def add(self, key_id: str, usd: float) -> None:
        await asyncio.sleep(0.02)
        await super().add(key_id, usd)

    async def reserve(self, key_id: str, usd: float, budget: float) -> bool:
        await asyncio.sleep(0.02)
        return await super().reserve(key_id, usd, budget)


@pytest.mark.usefixtures("registry", "priced")
async def test_a_burst_cant_overspend_with_a_slow_store(
    auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review (ADR 0023): with real store latency, check-then-reserve let a whole burst
    pass the same stale check. The reservation is now the check."""
    monkeypatch.setattr(services, "spend", SlowSpend())
    key = add_key(allowed_aliases=["*"], monthly_budget_usd=2500)  # ~2 calls' estimates
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gw", headers={"Authorization": f"Bearer {key}"}
    ) as gw:
        responses = await asyncio.gather(
            *(
                gw.post(
                    "/v1/chat/completions",
                    json={"model": "chaos/slow", "messages": MSGS, "max_tokens": 1000},
                )
                for _ in range(6)
            )
        )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) <= 3 and 429 in statuses
