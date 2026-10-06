"""Retries, fallback and circuit breakers through the real endpoint (ADR 0004, 0005)."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import RetryConfig
from app.providers.base import ProviderError
from app.routing import router
from app.routing.breaker import MemoryBreakerStore, State
from app.routing.router import Kind, backoff_seconds, classify
from tests.conftest import UPSTREAM

URL = f"{UPSTREAM}/chat/completions"
MSGS = [{"role": "user", "content": "hi"}]
OK = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "tiny",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
}


def post(client: TestClient, model: str, **extra: Any) -> httpx.Response:
    return client.post("/v1/chat/completions", json={"model": model, "messages": MSGS, **extra})


def attempts(resp: httpx.Response) -> int:
    return int(resp.headers["x-gateway-attempts"])


# --- classification and backoff -------------------------------------------

RETRY = RetryConfig()


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (ProviderError("x", status=400), Kind.CLIENT),
        (ProviderError("x", status=422), Kind.CLIENT),
        (ProviderError("x", status=401), Kind.GATEWAY),
        (ProviderError("x", status=404), Kind.GATEWAY),
        (ProviderError("x", status=429, code="insufficient_quota"), Kind.GATEWAY),
        (ProviderError("x", status=402, code="billing_error"), Kind.GATEWAY),
        (ProviderError("x", status=None), Kind.GATEWAY),  # e.g. provider not configured
        (ProviderError("x", status=429), Kind.TRANSIENT),
        (ProviderError("x", status=529), Kind.TRANSIENT),
        (ProviderError("x", timeout=True), Kind.TRANSIENT),
        (ProviderError("x", retryable=True), Kind.TRANSIENT),  # connection error
    ],
)
def test_classify(exc: ProviderError, kind: Kind) -> None:
    assert classify(exc, RETRY) is kind


def test_backoff_full_jitter_bounds() -> None:
    for attempt in range(6):
        cap = min(RETRY.backoff_max_ms, RETRY.backoff_base_ms * 2**attempt) / 1000
        for _ in range(50):
            delay = backoff_seconds(attempt, RETRY, ProviderError("x", status=503))
            assert delay is not None and 0 <= delay <= cap


def test_backoff_honours_short_retry_after_and_skips_long_ones() -> None:
    short = ProviderError("x", status=429, headers={"retry-after": "2"})
    long = ProviderError("x", status=429, headers={"retry-after": "120"})
    delay = backoff_seconds(0, RETRY, short)
    assert delay is not None and 2.0 <= delay <= 2.2  # honoured, plus ≤10% jitter
    assert backoff_seconds(0, RETRY, long) is None  # next target is the better bet


# --- retry & fallback --------------------------------------------------------


@respx.mock
def test_transient_failure_retried_on_same_target(client: TestClient) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=OK)])
    resp = post(client, "local")
    assert resp.status_code == 200
    assert route.call_count == 2 and attempts(resp) == 2
    assert resp.headers["x-gateway-fallback"] == "false"


def test_falls_back_when_primary_is_down(client: TestClient) -> None:
    resp = post(client, "down-then-ok")
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "chaos/ok"
    assert resp.headers["x-gateway-fallback"] == "true"
    assert attempts(resp) == 3  # 2 tries on the primary + 1 on the fallback


def test_gateway_fault_falls_back_without_retrying(client: TestClient) -> None:
    resp = post(client, "auth-then-ok")
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert attempts(resp) == 2  # 401 isn't retried: it won't fix itself


@respx.mock
def test_client_fault_is_returned_not_retried_or_fallen_back(client: TestClient) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "max_tokens too large"}})
    )
    resp = post(client, "mock-then-ok")
    assert resp.status_code == 400  # every provider would refuse it
    assert route.call_count == 1 and attempts(resp) == 1


@respx.mock
def test_untranslatable_request_falls_back_to_a_provider_that_can(client: TestClient) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    resp = post(client, "claude-then-mock", n=2)  # Anthropic can't do n>1
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "mock/tiny"
    assert route.call_count == 1


def test_all_targets_failing_returns_the_last_error(client: TestClient) -> None:
    resp = post(client, "only-down")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
    assert attempts(resp) == 2


# --- circuit breaker ---------------------------------------------------------


def test_breaker_opens_and_traffic_skips_the_sick_target(client: TestClient) -> None:
    for _ in range(3):  # one failure per request (retries don't add); threshold 3 in tests
        post(client, "down-then-ok")
    resp = post(client, "down-then-ok")
    assert resp.status_code == 200
    assert attempts(resp) == 1  # primary skipped: no wasted calls, no added latency
    assert resp.headers["x-gateway-fallback"] == "true"


def test_all_breakers_open_is_503(client: TestClient) -> None:
    for _ in range(3):
        post(client, "only-down")
    resp = post(client, "only-down")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "all_providers_unavailable"


@respx.mock
def test_breaker_recovers_through_half_open_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    store = MemoryBreakerStore(clock=lambda: now[0])
    monkeypatch.setattr(router, "store", store)
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    for _ in range(3):
        post(client, "mock-then-ok")
    assert store._open_until  # opened
    assert post(client, "mock-then-ok").headers["x-gateway-provider"] == "chaos/ok"

    route.mock(return_value=httpx.Response(200, json=OK))  # provider recovers
    now[0] += 31  # open_seconds pass → half-open
    resp = post(client, "mock-then-ok")
    assert resp.headers["x-gateway-provider"] == "mock/tiny"  # the probe went through
    assert post(client, "mock-then-ok").headers["x-gateway-fallback"] == "false"


def test_admin_shows_breaker_state(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import config

    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    for _ in range(3):
        post(client, "only-down")
    states = client.get(
        "/admin/providers", headers={"Authorization": "Bearer gw_admin_test"}
    ).json()["targets"]
    assert states["chaos/down"] == State.OPEN
    assert states["chaos/ok"] == State.CLOSED
    assert client.get("/admin/providers").status_code == 401


# --- streaming ---------------------------------------------------------------


def stream_lines(client: TestClient, model: str) -> tuple[httpx.Response, list[str]]:
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": model, "messages": MSGS, "stream": True}
    ) as resp:
        return resp, [ln for ln in resp.iter_lines() if ln]


def text_of(lines: list[str]) -> str:
    chunks = [json.loads(ln[6:]) for ln in lines if ln.startswith("data: {")]
    return "".join(
        c["choices"][0]["delta"].get("content") or "" for c in chunks if c.get("choices")
    )


def test_stream_falls_back_before_first_chunk(client: TestClient) -> None:
    resp, lines = stream_lines(client, "down-then-ok")
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert text_of(lines) == "Hello from fake/ok." and lines[-1] == "data: [DONE]"


def test_stream_failure_after_first_chunk_is_in_band_and_counted(client: TestClient) -> None:
    resp, lines = stream_lines(client, "broken")
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/broken-stream"
    assert "failed mid-stream" in lines[-1] and "data: [DONE]" not in lines  # ADR 0005
    for _ in range(2):
        stream_lines(client, "broken")
    # three mid-stream deaths open the breaker: the next stream goes to the fallback
    resp, lines = stream_lines(client, "broken")
    assert resp.headers["x-gateway-provider"] == "chaos/ok" and lines[-1] == "data: [DONE]"


def test_stream_total_deadline_ends_a_trickling_stream(client: TestClient) -> None:
    resp, lines = stream_lines(client, "trickle")  # 200ms per chunk, stream_total 0.3s
    assert resp.status_code == 200
    assert "exceeded its time limit" in lines[-1] and "data: [DONE]" not in lines


# --- review regressions --------------------------------------------------------


async def half_open(store: MemoryBreakerStore, now: list[float], target: str) -> None:
    from app.config import BreakerConfig
    from app.routing.breaker import Decision, Ticket

    cfg = BreakerConfig(failure_threshold=3, open_seconds=30)
    for _ in range(3):
        await store.record_failure(target, cfg, Ticket(Decision.ALLOW))
    now[0] += 31


@pytest.fixture
def clocked(monkeypatch: pytest.MonkeyPatch) -> tuple[MemoryBreakerStore, list[float]]:
    now = [1000.0]
    store = MemoryBreakerStore(clock=lambda: now[0])
    monkeypatch.setattr(router, "store", store)
    return store, now


@respx.mock
async def test_probe_slot_released_when_request_is_unsupported(
    client: TestClient, clocked: tuple[MemoryBreakerStore, list[float]]
) -> None:
    store, now = clocked
    await half_open(store, now, "claude/old")
    respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    post(client, "claude-then-mock", n=2)  # probe on claude can't express n>1
    assert await store.state("claude/old") is State.HALF_OPEN
    assert "claude/old" not in store._probe  # slot handed back, not held for 330s


@respx.mock
async def test_client_fault_on_a_probe_closes_the_breaker(
    client: TestClient, clocked: tuple[MemoryBreakerStore, list[float]]
) -> None:
    store, now = clocked
    await half_open(store, now, "mock/tiny")
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    assert post(client, "local").status_code == 400
    assert await store.state("mock/tiny") is State.CLOSED  # it answered: it's healthy


async def test_open_target_plus_unsupported_request_is_retryable_503(
    client: TestClient, clocked: tuple[MemoryBreakerStore, list[float]]
) -> None:
    store, _ = clocked
    from app.config import BreakerConfig
    from app.routing.breaker import Decision, Ticket

    for _ in range(3):
        await store.record_failure(
            "mock/tiny", BreakerConfig(failure_threshold=3), Ticket(Decision.ALLOW)
        )
    resp = post(client, "claude-then-mock", n=2)  # claude can't; mock is open
    assert resp.status_code == 503  # not a 400: the request is fine, retry later
    assert resp.json()["error"]["code"] == "all_providers_unavailable"


@respx.mock
def test_provider_failure_outranks_unsupported_request(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(503))
    from app import config

    config.registry.aliases["mock-then-claude"] = config.registry.aliases["local"].model_copy(
        update={"chain": ["mock/tiny", "claude/old"]}
    )
    resp = post(client, "mock-then-claude", n=2)
    assert resp.status_code == 502  # the outage is the real reason, not n>1
    assert attempts(resp) == 2  # the untranslatable claude "attempt" isn't a call


def test_unknown_direct_model_is_404_without_breaker_state(client: TestClient) -> None:
    resp = post(client, "mock/made-up-model-123")
    assert resp.status_code == 404
    assert "mock/made-up-model-123" not in router.store._fails  # type: ignore[attr-defined]
