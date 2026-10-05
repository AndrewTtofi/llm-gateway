# 0008 — Observability: usage log, metrics, logs

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 5

## Context
Operators need to see what every request cost and how every provider behaves — spend
per key, latency and time-to-first-token per provider, fallback and error rates,
breaker states — without slowing requests down or leaking prompts.

## Decision
**Two stores, each for what it's good at.**
- **Prometheus** for operational signals, labelled only by bounded sets: `alias` (a
  configured alias or known target, else `_unknown` — never raw client input), `target`
  (provider/model), `status` (HTTP code, plus our own error code for failures),
  attempt `outcome` (kind + HTTP status, or timeout/network — never a provider-supplied
  code), `reason`. Never by API key: keys are unbounded, and every label value is a
  separate time series.
- **Postgres `usage_log`** — one row per admitted request: request id, key, alias,
  target, provider, model, tokens (prompt / completion / cached), cost, latency, TTFT,
  attempts, fallback, status, error code, streamed. Spend per key is a SQL `SUM`, exact
  and unbounded in keys. Grafana reads it through a Postgres datasource.

**Writes never block requests.** Rows go into an in-process queue; a background task
inserts them in batches (every second or 100 rows). If the queue is full or Postgres is
down, rows are dropped and counted (`gateway_usage_log_dropped_total`) — a slow database
must not become slow requests. A failed batch is retried row by row, so one bad row
can't take 99 others with it; values are truncated to their column sizes. Shutdown
finishes the batch in progress and flushes the queue. Every admitted request gets
exactly one row, even when Redis fails while settling it. The Redis month-to-date counters still enforce budgets;
the log is the audit trail and the source for reports.

**Metrics** (all prefixed `gateway_`): requests by alias/target/status, request duration
and TTFT histograms per target, tokens by target and kind, cost by target, fallbacks by
alias, upstream attempts by target and outcome, rate-limit rejections by reason,
breaker state per target (polled from the store every 15 s). Per-target histograms
measure only the attempt that served — a healthy fallback isn't charged for the failed
provider's timeouts — while the usage row keeps the client's end-to-end latency and
TTFT. Streams are measured to the end of the stream, TTFT to the first chunk. A client
leaving mid-stream is recorded as 499 `client_disconnected`, not a clean 200; the
dashboard's error rate counts gateway failures (5xx, in-band stream errors), not client
errors.

**Logs**: JSON via structlog: an `http` access line per HTTP request (method, path,
status, ms) and a `request` usage line per admitted chat request (key prefix, alias,
target, status, tokens, cost, latency) — never prompt or completion content.
Request id from a valid incoming `x-request-id`, else a new UUID; echoed in the response.

**Cost**: `pricing.yaml` gains an optional `cached_input` price. Cached prompt tokens are
billed at it (Anthropic cache reads are ~10% of input), otherwise at `input`.

**`/metrics`** is unauthenticated (Prometheus convention) and reveals traffic shape and
spend per model, not keys or content. It is served on its **own port** (`metrics_port`,
9100) so it can be firewalled off while the API port stays public; `/metrics` on the
API port is a 404. (`metrics_port: 0` serves it on the API port for single-port setups.)
One metrics port per process: scale with replicas, not uvicorn workers.

## Consequences
- The usage log can lose rows during a Postgres outage (or a shutdown that exceeds 10 s);
  budgets (Redis) still hold.
- Cache *writes* (1.25× on Anthropic) are priced as normal input — a small undercount.
- OpenTelemetry traces are left for later; request ids already correlate logs and rows.
