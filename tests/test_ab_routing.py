"""A/B routing: weighted, sticky variants with optional system-prompt prefixes (ADR 0020)."""

import json
from collections import Counter
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import services
from app.auth import ApiKey
from app.config import Alias, Registry, Variant
from app.routing import ab
from tests.conftest import UPSTREAM, add_key
from tests.test_chat import COMPLETION

URL = f"{UPSTREAM}/chat/completions"
MSGS = [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}]


@pytest.fixture
def experiment(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Alias:
    alias = Alias(
        sticky="key",
        allow_pin=True,  # the tests pin arms; off by default
        variants=[
            Variant(name="control", weight=50, chain=["mock/tiny"]),
            Variant(name="concise", weight=50, chain=["chaos/ok"], system_prefix="Answer briefly."),
        ],
    )
    monkeypatch.setitem(registry.aliases, "exp", alias)
    return alias


def key(i: int) -> ApiKey:
    return ApiKey(id=f"key-{i}", name="n", prefix="gw_x", tier="dev")


def test_split_follows_the_weights() -> None:
    alias = Alias(
        variants=[
            Variant(name="a", weight=90, chain=["x/y"]),
            Variant(name="b", weight=10, chain=["x/z"]),
        ]
    )
    counts = Counter(ab.assign("exp", alias, key(i), None, None).name for i in range(5000))
    assert 0.87 < counts["a"] / 5000 < 0.93


def test_assignment_is_sticky_per_key_and_per_user() -> None:
    alias = Alias(
        variants=[
            Variant(name="a", weight=1, chain=["x/y"]),
            Variant(name="b", weight=1, chain=["x/z"]),
        ]
    )
    assert len({ab.assign("exp", alias, key(7), None, None).name for _ in range(50)}) == 1
    by_user = Alias(sticky="user", variants=alias.variants)
    choices = {ab.assign("exp", by_user, key(1), "alice", None).name for _ in range(50)}
    assert len(choices) == 1
    # different aliases split independently (the alias is part of the hash)
    names = [
        (
            ab.assign("e1", alias, key(i), None, None).name,
            ab.assign("e2", alias, key(i), None, None).name,
        )
        for i in range(200)
    ]
    assert any(x != y for x, y in names)


def test_pinning_a_variant() -> None:
    alias = Alias(
        variants=[
            Variant(name="a", weight=1000, chain=["x/y"]),
            Variant(name="b", weight=1, chain=["x/z"]),
        ]
    )
    assert ab.assign("exp", alias, key(1), None, "b").name == "b"
    assert ab.assign("exp", alias, key(1), None, "nonexistent").name in ("a", "b")


def test_prefix_goes_in_front_of_the_system_prompt() -> None:
    from app.schemas import ChatCompletionRequest

    with_system = ab.with_prefix(ChatCompletionRequest(model="m", messages=MSGS), "Be brief.")
    assert with_system.model_dump()["messages"][0]["content"] == "Be brief.\n\nBe helpful."
    without = ab.with_prefix(
        ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "q"}]), "Be brief."
    )
    assert without.model_dump()["messages"][0] == {"role": "system", "content": "Be brief."}


@respx.mock
def test_variant_chain_prefix_header_and_usage(client: TestClient, experiment: Alias) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "exp", "messages": MSGS},
        headers={"x-gateway-variant": "control"},
    )
    assert (
        resp.headers["x-gateway-variant"] == "control"
        and resp.headers["x-gateway-provider"] == "mock/tiny"
    )
    assert json.loads(route.calls.last.request.content)["messages"][0]["content"] == "Be helpful."
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "exp", "messages": MSGS},
        headers={"x-gateway-variant": "concise"},
    )
    assert (
        resp.headers["x-gateway-variant"] == "concise"
        and resp.headers["x-gateway-provider"] == "chaos/ok"
    )
    rows = services.usage.records  # type: ignore[attr-defined]
    assert [r.variant for r in rows[-2:]] == ["control", "concise"]


def test_variant_metrics(client: TestClient, experiment: Alias) -> None:
    from app.observability import metrics

    client.post(
        "/v1/chat/completions",
        json={"model": "exp", "messages": MSGS},
        headers={"x-gateway-variant": "concise"},
    )
    value = metrics.registry.get_sample_value(
        "gateway_variant_requests_total", {"alias": "exp", "variant": "concise", "status": "200"}
    )
    assert value and value >= 1


@respx.mock
def test_each_arm_has_its_own_cache_entries(
    client: TestClient, experiment: Alias, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import cache
    from app.config import CacheConfig

    monkeypatch.setattr(services, "response_cache", cache.MemoryCacheStore())
    monkeypatch.setattr(experiment, "cache", CacheConfig(scope="global"))
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    first = client.post(
        "/v1/chat/completions",
        json={"model": "exp", "messages": MSGS},
        headers={"x-gateway-variant": "control"},
    )
    other = client.post(
        "/v1/chat/completions",
        json={"model": "exp", "messages": MSGS},
        headers={"x-gateway-variant": "concise"},
    )
    assert first.headers["x-gateway-cache"] == "miss" and other.headers["x-gateway-cache"] == "miss"


def test_variants_are_listed(client: TestClient, experiment: Alias) -> None:
    models = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert [v["name"] for v in models["exp"]["variants"]] == ["control", "concise"]
    assert models["exp"]["sticky"] == "key"


@pytest.mark.parametrize(
    "spec",
    [
        {"variants": [{"name": "a", "weight": 1, "chain": ["x/y"]}], "chain": ["x/y"]},
        {
            "variants": [
                {"name": "a", "weight": 1, "chain": ["x/y"]},
                {"name": "a", "weight": 1, "chain": ["x/z"]},
            ]
        },
        {"variants": [{"name": "Bad Name!", "weight": 1, "chain": ["x/y"]}]},
        {"variants": [{"name": "a", "weight": 0, "chain": ["x/y"]}]},
    ],
)
def test_invalid_experiments_are_rejected(spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Alias.model_validate(spec)


def test_split_across_real_keys(registry: Registry, experiment: Alias) -> None:
    from app import main

    seen = Counter()
    for _ in range(40):
        k = add_key(allowed_aliases=["*"])
        with TestClient(main.app, headers={"Authorization": f"Bearer {k}"}) as c:
            seen[
                c.post("/v1/chat/completions", json={"model": "exp", "messages": MSGS}).headers[
                    "x-gateway-variant"
                ]
            ] += 1
    assert seen["control"] and seen["concise"]  # both arms get traffic


def test_pinning_is_ignored_unless_the_alias_allows_it(
    client: TestClient, experiment: Alias, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(experiment, "allow_pin", False)
    seen = {
        client.post(
            "/v1/chat/completions",
            json={"model": "exp", "messages": MSGS},
            headers={"x-gateway-variant": pin},
        ).headers["x-gateway-variant"]
        for pin in ("control", "concise")
    }
    assert len(seen) == 1  # the caller's own sticky arm, whatever it asks for
