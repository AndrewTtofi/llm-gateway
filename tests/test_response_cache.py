"""Response cache: exact and semantic, scopes, streams, bypass/refresh (ADR 0018)."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import cache, main, services
from app.config import CacheConfig, Registry
from tests.conftest import UPSTREAM, add_key
from tests.test_chat import COMPLETION, chunk, sse

URL = f"{UPSTREAM}/chat/completions"
EMBED = f"{UPSTREAM}/embeddings"
MSGS = [{"role": "user", "content": "What is the capital of France?"}]


@pytest.fixture
def cached(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> CacheConfig:
    monkeypatch.setattr(services, "response_cache", cache.MemoryCacheStore())
    cfg = CacheConfig(mode="exact", ttl_seconds=60)
    monkeypatch.setattr(registry.aliases["local"], "cache", cfg)
    return cfg


def ask(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "local", "messages": MSGS, **body},
        headers=headers or {},
    )


@respx.mock
def test_exact_hit_skips_the_provider(client: TestClient, cached: CacheConfig) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    first = ask(client)
    assert first.headers["x-gateway-cache"] == "miss" and route.call_count == 1
    second = ask(client)
    assert second.status_code == 200 and route.call_count == 1  # served from cache
    assert second.headers["x-gateway-cache"] == "hit"
    assert second.headers["x-gateway-provider"] == "cache/exact"
    assert second.json()["choices"][0]["message"]["content"] == "hello"
    rows = services.usage.records  # type: ignore[attr-defined]
    assert rows[-1].target == "cache/exact" and rows[-1].cost_usd == 0


@respx.mock
def test_different_parameters_are_different_entries(
    client: TestClient, cached: CacheConfig
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    ask(client, temperature=0)
    ask(client, temperature=1)
    ask(client, temperature=0, user="someone-else")  # `user` doesn't change the answer
    assert route.call_count == 2


@respx.mock
def test_bypass_and_refresh(client: TestClient, cached: CacheConfig) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    ask(client, headers={"x-gateway-cache": "bypass"})
    assert ask(client).headers["x-gateway-cache"] == "miss"  # bypass didn't store
    assert ask(client).headers["x-gateway-cache"] == "hit"
    refreshed = ask(client, headers={"x-gateway-cache": "refresh"})
    assert refreshed.headers["x-gateway-cache"] == "refresh"
    assert route.call_count == 3


@respx.mock
def test_scope_key_isolates_callers(
    client: TestClient, cached: CacheConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    ask(client)
    other = {"Authorization": f"Bearer {add_key(allowed_aliases=['*'])}"}
    assert ask(client, headers=other).headers["x-gateway-cache"] == "miss"  # not shared
    monkeypatch.setattr(cached, "scope", "global")
    ask(client)
    assert ask(client, headers=other).headers["x-gateway-cache"] == "hit"  # shared on purpose


@respx.mock
def test_incomplete_answers_are_not_stored(client: TestClient, cached: CacheConfig) -> None:
    cut = {**COMPLETION, "choices": [{**COMPLETION["choices"][0], "finish_reason": "length"}]}
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=cut))
    ask(client)
    ask(client)
    assert route.call_count == 2


@respx.mock
def test_streams_are_stored_and_replayed(client: TestClient, cached: CacheConfig) -> None:
    usage = {
        **chunk(),
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200, content=sse(chunk("Par"), chunk("is"), chunk(finish="stop"), usage, "[DONE]")
        )
    )

    def stream() -> tuple[str, httpx.Headers]:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "local", "messages": MSGS, "stream": True},
        ) as r:
            lines = [line for line in r.iter_lines() if line.startswith("data: ")]
            headers = r.headers
        assert lines[-1] == "data: [DONE]"
        text = "".join(
            (json.loads(line[6:])["choices"][0]["delta"].get("content") or "")
            for line in lines[:-1]
            if json.loads(line[6:]).get("choices")
        )
        return text, headers

    first, h1 = stream()
    second, h2 = stream()
    assert first == second == "Paris"
    assert h1["x-gateway-cache"] == "miss" and h2["x-gateway-cache"] == "hit"
    assert route.call_count == 1
    # a stored stream also serves a non-streamed request
    assert ask(client).json()["choices"][0]["message"]["content"] == "Paris"


@respx.mock
def test_hits_still_count_against_rate_limits(registry: Registry, cached: CacheConfig) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    key = add_key(requests_per_minute=2, allowed_aliases=["*"])
    with TestClient(main.app, headers={"Authorization": f"Bearer {key}"}) as c:
        assert ask(c).status_code == 200
        assert ask(c).status_code == 200  # hit
        assert ask(c).status_code == 429


@respx.mock
def test_anthropic_clients_get_cached_answers_in_their_format(
    client: TestClient, cached: CacheConfig
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    body = {"model": "local", "max_tokens": 50, "messages": MSGS}
    client.post("/v1/messages", json=body)
    hit = client.post("/v1/messages", json=body)
    assert hit.headers["x-gateway-cache"] == "hit" and hit.json()["content"][0]["text"] == "hello"


# --- semantic ------------------------------------------------------------------------------


def embedding_for(text: str) -> list[float]:
    """Toy embeddings: France questions point one way, anything else another."""
    return [1.0, 0.1, 0.0] if "France" in text or "french" in text.lower() else [0.0, 0.2, 1.0]


@pytest.fixture
def semantic(cached: CacheConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cached, "mode", "semantic")
    monkeypatch.setattr(cached, "embedding", "mock/embedder")
    monkeypatch.setattr(cached, "threshold", 0.95)

    def embed(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": embedding_for(t)} for i, t in enumerate(texts)]
            },
        )

    respx.post(EMBED).mock(side_effect=embed)


@respx.mock
def test_semantic_hit_for_a_similar_question(client: TestClient, semantic: None) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    ask(client)
    similar = [{"role": "user", "content": "Tell me France's capital city"}]
    hit = client.post("/v1/chat/completions", json={"model": "local", "messages": similar})
    assert hit.headers["x-gateway-cache"] == "hit" and route.call_count == 1
    different = [{"role": "user", "content": "How tall is Everest?"}]
    miss = client.post("/v1/chat/completions", json={"model": "local", "messages": different})
    assert miss.headers["x-gateway-cache"] == "miss" and route.call_count == 2


@respx.mock
def test_embedding_failure_falls_back_to_exact(
    client: TestClient, cached: CacheConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cached, "mode", "semantic")
    monkeypatch.setattr(cached, "embedding", "mock/embedder")
    respx.post(EMBED).mock(return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    assert ask(client).status_code == 200
    assert ask(client).headers["x-gateway-cache"] == "hit"  # exact still works
    assert route.call_count == 1


# --- units -------------------------------------------------------------------------------


def test_request_hash_ignores_transport_fields() -> None:
    a = cache.request_hash("x", {"messages": MSGS, "stream": True, "user": "u", "route": {}})
    b = cache.request_hash("x", {"messages": MSGS, "stream": False})
    assert a == b and a != cache.request_hash("y", {"messages": MSGS})


def test_replay_round_trips_tool_calls() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
    result = {
        "id": "x",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [call]},
            }
        ],
    }
    collector = cache.Collector()
    for c in cache.replay(result):
        collector.feed(c)
    assert collector.result()["choices"][0]["message"]["tool_calls"] == [call]
    assert collector.result()["choices"][0]["finish_reason"] == "tool_calls"


def test_oversized_answers_are_not_stored() -> None:
    import asyncio

    store = cache.MemoryCacheStore()
    cfg = CacheConfig(max_entry_bytes=1024)
    lookup = cache.Lookup(cfg, store, "s", "h", "i", None, write=True)
    big = {
        **COMPLETION,
        "choices": [
            {**COMPLETION["choices"][0], "message": {"role": "assistant", "content": "x" * 5000}}
        ],
    }
    assert asyncio.run(lookup.save(big)) is False


async def test_redis_store_round_trip() -> None:
    from tests.test_breaker import redis_or_skip

    redis = await redis_or_skip()
    store = cache.RedisCacheStore(redis)
    try:
        await store.set("t:1", {"a": 1}, ttl=30)
        assert await store.get("t:1") == {"a": 1}
        assert await store.similar("t-index", [1.0, 0.0, 0.0], 0.9) is None  # no index yet
        await store.add_vector("t-index", [1.0, 0.0, 0.0], "e1", max_entries=10)
        assert await store.similar("t-index", [0.99, 0.01, 0.0], 0.9) == "e1"
        assert await store.similar("t-index", [0.0, 0.0, 1.0], 0.9) is None
        await store.forget("t-index", "e1")
        assert await store.similar("t-index", [1.0, 0.0, 0.0], 0.9) is None
    finally:
        await redis.delete("rcache:t:1", "vcache:t-index")
        await redis.aclose()
