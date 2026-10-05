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
    "Time to first token (streams).",
    ["target"],
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
)  # unauthenticated / model_not_allowed / rate_limit / budget
breaker = Gauge(
    "gateway_circuit_state",
    "Circuit breaker per target: 0 closed, 1 half-open, 2 open.",
    ["target"],
    registry=registry,
)
usage_dropped = Counter(
    "gateway_usage_log_dropped_total",
    "Usage rows dropped (queue full or Postgres down).",
    registry=registry,
)

BREAKER_VALUE = {"closed": 0, "half_open": 1, "open": 2}
