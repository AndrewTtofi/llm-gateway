"""Prometheus metrics (ADR 0008). Labels are bounded sets only — never API keys."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

registry = CollectorRegistry()

# LLM latencies span milliseconds (cache, errors) to minutes (long generations).
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300, 600)
TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 15, 30, 60)

requests = Counter(
    "gateway_requests_total",
    "Admitted chat requests by outcome.",
    ["alias", "target", "status"],
    registry=registry,
)
duration = Histogram(
    "gateway_request_duration_seconds",
    "Request time, to the end of the stream.",
    ["target", "stream"],
    buckets=LATENCY_BUCKETS,
    registry=registry,
)
ttft = Histogram(
    "gateway_ttft_seconds",
    "Time to first token of the attempt that served (streams) — per-target debugging.",
    ["target"],
    buckets=TTFT_BUCKETS,
    registry=registry,
)
ttft_e2e = Histogram(
    "gateway_ttft_e2e_seconds",
    "Time to first token as the client sees it: from the request arriving, including "
    "admission, failed attempts and fallback (streams). The TTFT SLO uses this.",
    buckets=TTFT_BUCKETS,
    registry=registry,
)
tokens = Counter(
    "gateway_tokens_total",
    "Tokens by target and kind (prompt includes cached).",
    ["target", "kind"],
    registry=registry,
)
cost = Counter("gateway_cost_usd_total", "Spend in USD by target.", ["target"], registry=registry)
fallbacks = Counter(
    "gateway_fallbacks_total",
    "Requests served by a target other than the chain's first.",
    ["alias", "target"],
    registry=registry,
)
attempts = Counter(
    "gateway_upstream_attempts_total",
    "Upstream calls by target and outcome.",
    ["target", "outcome"],
    registry=registry,
)
rejected = Counter(
    "gateway_rejected_total", "Requests rejected before routing.", ["reason"], registry=registry
)  # unauthenticated / model_not_allowed / rate_limit / budget / concurrency …
breaker = Gauge(
    "gateway_circuit_state",
    "Circuit breaker per target: 0 closed, 1 half-open, 2 open.",
    ["target"],
    registry=registry,
)
fail_open = Counter(
    "gateway_redis_fail_open_total",
    "Calls answered without Redis while it was unreachable: rate limits allow, budgets "
    "read the last known spend, and spend is queued until Redis is back (ADR 0023).",
    ["what"],
    registry=registry,
)
usage_dropped = Counter(
    "gateway_usage_log_dropped_total",
    "Usage rows dropped (queue full or Postgres down).",
    registry=registry,
)

BREAKER_VALUE = {"closed": 0, "half_open": 1, "open": 2}
loop_lag = Histogram(
    "gateway_event_loop_lag_seconds",
    "How late a 0.5 s timer fires: time the event loop was blocked or saturated.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
    registry=registry,
)
auth_stale = Counter(
    "gateway_auth_stale_served_total",
    "Requests authenticated from a stale cached key because the key store was down.",
    registry=registry,
)
cache = Counter(
    "gateway_cache_total",
    "Response cache lookups by mode and result: hit_exact, hit_semantic, miss, store, "
    "bypass, refresh (ADR 0018).",
    ["mode", "result"],
    registry=registry,
)
probes = Counter(
    "gateway_probes_total",
    "Background probes of half-open targets: recovered, failed, skipped, busy (ADR 0019).",
    ["target", "result"],
    registry=registry,
)
alerts = Counter(
    "gateway_alerts_total",
    "Alert webhook posts: sent, failed (ADR 0019).",
    ["result"],
    registry=registry,
)
variant_requests = Counter(
    "gateway_variant_requests_total",
    "Requests per A/B arm and outcome (ADR 0020); variant names come from config.",
    ["alias", "variant", "status"],
    registry=registry,
)
variant_duration = Histogram(
    "gateway_variant_duration_seconds",
    "End-to-end request time per A/B arm.",
    ["alias", "variant"],
    buckets=LATENCY_BUCKETS,
    registry=registry,
)
variant_cost = Counter(
    "gateway_variant_cost_usd_total", "Spend per A/B arm.", ["alias", "variant"], registry=registry
)
guardrail = Counter(
    "gateway_guardrail_detections_total",
    "Prompt-injection detections by rule (from config) and action (ADR 0021); rule "
    "\"unscanned\" counts requests with text over the scan budget (ADR 0023).",
    ["rule", "action"],
    registry=registry,
)
judge = Counter(
    "gateway_judge_total",
    "LLM-as-judge samples by result: scored, dropped, error, unparsable (ADR 0022).",
    ["alias", "result"],
    registry=registry,
)
judge_score = Histogram(
    "gateway_judge_score",
    "Judge scores (1 bad … 5 excellent) per alias and A/B variant.",
    ["alias", "variant"],
    buckets=(1.5, 2.5, 3.5, 4.5, 5.5),
    registry=registry,
)
