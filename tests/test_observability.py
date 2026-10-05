"""Phase 5: metrics, usage records, request ids, logs (ADR 0008)."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config, services
from app.observability import logging as obs_log
from app.observability import metrics
from app.observability.usage import MemoryUsageSink, UsageRecord

MSGS = [{"role": "user", "content": "hi"}]


def sample(name: str, **labels: str) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


def records() -> list[UsageRecord]:
    sink = services.usage
    assert isinstance(sink, MemoryUsageSink)
    return sink.records


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "pricing",
        config.Pricing.model_validate(
            {"models": {"chaos/ok": {"input": 1e6, "output": 1e6, "cached_input": 1e5}}}
        ),
    )


def chat(client: TestClient, model: str = "chaos/ok", **extra: Any) -> Any:
    return client.post("/v1/chat/completions", json={"model": model, "messages": MSGS, **extra})


# --- metrics -------------------------------------------------------------------


def test_metrics_endpoint_is_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200 and "gateway_requests_total" in resp.text


def test_request_counts_tokens_and_cost(client: TestClient, priced: None) -> None:
    before = sample("gateway_requests_total", alias="chaos/ok", target="chaos/ok", status="200")
    cost_before = sample("gateway_cost_usd_total", target="chaos/ok")
    chat(client)
    assert (
        sample("gateway_requests_total", alias="chaos/ok", target="chaos/ok", status="200")
        == before + 1
    )
    assert sample("gateway_cost_usd_total", target="chaos/ok") > cost_before
    assert sample("gateway_tokens_total", target="chaos/ok", kind="completion") > 0
    assert sample("gateway_request_duration_seconds_count", target="chaos/ok", stream="false") >= 1


def test_fallbacks_and_attempts_are_counted(client: TestClient) -> None:
    before = sample("gateway_fallbacks_total", alias="down-then-ok", target="chaos/ok")
    chat(client, "down-then-ok")
    assert sample("gateway_fallbacks_total", alias="down-then-ok", target="chaos/ok") == before + 1
    assert (
        sample("gateway_upstream_attempts_total", target="chaos/down", outcome="transient:503") >= 2
    )


def test_ttft_is_recorded_for_streams(client: TestClient) -> None:
    before = sample("gateway_ttft_seconds_count", target="chaos/ok")
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS, "stream": True}
    ) as r:
        list(r.iter_lines())
    assert sample("gateway_ttft_seconds_count", target="chaos/ok") == before + 1


def test_rejections_counted_by_reason(client: TestClient) -> None:
    before = sample("gateway_rejected_total", reason="unauthenticated")
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gw_nope"},
        json={"model": "chaos/ok", "messages": MSGS},
    )
    assert sample("gateway_rejected_total", reason="unauthenticated") == before + 1


def test_no_api_key_label_anywhere(client: TestClient) -> None:
    chat(client)
    text = client.get("/metrics").text
    assert "gw_" not in text and 'key="' not in text  # unbounded labels stay out


# --- usage records ---------------------------------------------------------------


def test_one_record_per_request_with_the_details(client: TestClient, priced: None) -> None:
    chat(client, "down-then-ok", user="bob@example.com")
    r = records()[-1]
    assert (r.alias, r.target, r.status, r.fallback, r.streamed) == (
        "down-then-ok",
        "chaos/ok",
        200,
        True,
        False,
    )
    assert r.attempts == 3 and r.completion_tokens > 0 and not r.usage_estimated
    assert r.cost_usd and r.cost_usd > 0 and r.latency_ms >= 0 and r.ttft_ms is None


def test_stream_records_ttft_and_in_band_errors(client: TestClient) -> None:
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "broken", "messages": MSGS, "stream": True}
    ) as r:
        list(r.iter_lines())
    rec = records()[-1]
    assert rec.streamed and rec.status == 200 and rec.error_code == "upstream_error"
    assert rec.ttft_ms is not None


def test_failed_request_is_recorded_without_cost(client: TestClient, priced: None) -> None:
    chat(client, "only-down")
    rec = records()[-1]
    assert rec.status == 502 and rec.error_code == "upstream_error"
    assert rec.target is None and rec.cost_usd is None or rec.cost_usd == 0


def test_cached_tokens_are_cheaper() -> None:
    p = config.Pricing.model_validate(
        {"models": {"m": {"input": 10, "output": 0, "cached_input": 1}}}
    )
    assert p.cost("m", 1000, 0, cached_tokens=900) == pytest.approx((100 * 10 + 900 * 1) / 1e6)
    assert p.cost("m", 1000, 0) == pytest.approx(1000 * 10 / 1e6)


# --- request ids & logs ------------------------------------------------------------


def test_request_id_echoed_reused_or_replaced(client: TestClient) -> None:
    generated = chat(client).headers["x-request-id"]
    assert len(generated) == 32
    reused = client.post(
        "/v1/chat/completions",
        headers={"x-request-id": "trace-abc.123"},
        json={"model": "chaos/ok", "messages": MSGS},
    )
    assert reused.headers["x-request-id"] == "trace-abc.123"
    assert records()[-1].request_id == "trace-abc.123"
    junk = client.post(
        "/v1/chat/completions",
        headers={"x-request-id": "x" * 500},
        json={"model": "chaos/ok", "messages": MSGS},
    )
    assert junk.headers["x-request-id"] != "x" * 500


def test_logs_are_json_with_request_id_and_never_content(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    obs_log.configure("INFO")
    marker = "PROMPT-MARKER-7f3a"
    client.post(
        "/v1/chat/completions",
        headers={"x-request-id": "rid-logtest"},
        json={"model": "chaos/ok", "messages": [{"role": "user", "content": marker}]},
    )
    lines = [json.loads(ln) for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]
    usage = [ln for ln in lines if ln.get("logger") == "gateway.usage"]
    assert usage and usage[-1]["request_id"] == "rid-logtest"
    assert usage[-1]["target"] == "chaos/ok" and "completion_tokens" in usage[-1]
    assert all(marker not in json.dumps(ln) for ln in lines)
    assert all("Hello from fake" not in json.dumps(ln) for ln in lines)  # nor the answer


# --- review regressions ---------------------------------------------------------------


def test_unknown_model_is_a_404_record_with_a_fixed_label(client: TestClient) -> None:
    resp = chat(client, "x" * 250)
    assert resp.status_code == 404
    rec = records()[-1]
    assert (rec.status, rec.error_code) == (404, "model_not_found")
    text = client.get("/metrics").text
    assert "x" * 250 not in text  # client input never becomes a label
    assert (
        sample("gateway_requests_total", alias="_unknown", target="", status="404:model_not_found")
        >= 1
    )


def test_overlong_model_name_is_rejected_up_front(client: TestClient) -> None:
    assert chat(client, "x" * 300).status_code == 400


def test_failed_request_is_not_a_fallback(client: TestClient) -> None:
    before = sample("gateway_fallbacks_total", alias="only-down", target="")
    chat(client, "only-down")
    assert records()[-1].fallback is False
    assert sample("gateway_fallbacks_total", alias="only-down", target="") == before


@pytest.mark.usefixtures("registry")
async def test_mid_stream_disconnect_is_a_499_not_a_clean_200() -> None:
    """TestClient buffers streams, so drive the ASGI app directly and hang up after the
    first chunk while the upstream is still generating."""
    import asyncio

    import httpx

    from tests.test_disconnect import HangingStream, call_app, use_transport

    use_transport(lambda req: httpx.Response(200, stream=HangingStream()))
    hang_up = asyncio.Event()
    task = asyncio.create_task(
        call_app({"model": "local", "messages": MSGS, "stream": True}, hang_up)
    )
    await asyncio.sleep(0.2)
    hang_up.set()
    await task
    rec = records()[-1]
    assert (rec.status, rec.error_code, rec.target) == (499, "client_disconnected", "mock/tiny")


def test_completed_stream_is_a_clean_200(client: TestClient) -> None:
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS, "stream": True}
    ) as r:
        list(r.iter_lines())
    assert (records()[-1].status, records()[-1].error_code) == (200, None)


def test_request_is_recorded_even_if_redis_fails_while_settling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*a: Any, **k: Any) -> None:
        raise ConnectionError("redis gone")

    monkeypatch.setattr(services.limiter, "adjust", broken)
    n = len(records())
    assert chat(client).status_code == 200
    assert len(records()) == n + 1


def test_unexpected_error_is_recorded_as_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.metering import Meter

    def boom(self: Meter, result: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(Meter, "observe_completion", boom)
    with pytest.raises(RuntimeError):
        chat(client)
    assert (records()[-1].status, records()[-1].error_code) == (500, "gateway_error")


def test_fallback_latency_is_charged_to_the_target_that_served(client: TestClient) -> None:
    # down-then-ok: two failed tries on chaos/down first; chaos/ok's own histogram must
    # not include them. Compare the end-to-end row latency with the per-target sample.
    before_sum = sample("gateway_request_duration_seconds_sum", target="chaos/ok", stream="false")
    chat(client, "down-then-ok")
    observed = (
        sample("gateway_request_duration_seconds_sum", target="chaos/ok", stream="false")
        - before_sum
    )
    assert observed * 1000 <= records()[-1].latency_ms + 1


async def test_one_unpriced_attempt_doesnt_make_the_request_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth import ApiKey
    from app.metering import Meter
    from app.ratelimit import MemoryLimiter, MemorySpend

    monkeypatch.setattr(
        config,
        "pricing",
        config.Pricing.model_validate({"models": {"claude/old": {"input": 1e6, "output": 1e6}}}),
    )
    spend, key = MemorySpend(), ApiKey(id="k", name="n", prefix="p", tier="dev")
    m = Meter(key, key.limits(config.limits), MemoryLimiter(), spend, 10, 5, False)
    m.target = "claude/old"
    m.usage = {
        "iterations": [
            {"model": "old", "input_tokens": 3, "output_tokens": 2},
            {"model": "claude-unpriced-dated", "input_tokens": 3},
        ]
    }
    await m.settle()
    assert await spend.spent("k") == pytest.approx(5.0)  # the priced attempt is billed
