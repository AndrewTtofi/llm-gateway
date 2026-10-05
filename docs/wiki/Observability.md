# Observability

ADR 0008. The gateway produces three signals, each for a different question:

| Signal | Question it answers | Where |
|--------|--------------------|-------|
| Prometheus metrics | Is it healthy right now? Which provider is slow or failing? | `:9100/metrics` → Prometheus → Grafana |
| Usage log | Exactly who spent what, request by request? | Postgres `usage_log` |
| JSON logs | What happened to *this* request? | stdout, correlated by `request_id` |

## Metrics

Served on a **separate internal port** (`METRICS_PORT`, default 9100), so `/metrics` can be
firewalled while the API stays public. On the API port, `/metrics` is a 404 unless
`METRICS_PORT=0`.

Labels come only from **bounded sets**: configured aliases, targets, status codes, outcomes
and reasons. They never use API keys or client-supplied model names. Every label value
creates a new time series, and an unbounded label (one per key or per typo'd model)
eventually takes Prometheus down. Unknown models are labelled `_unknown`.

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `gateway_requests_total` | counter | alias, target, status | Admitted chat requests by outcome. `status` is `200`, `4xx`/`5xx`, or `200:<code>` for a stream that failed after the 200 |
| `gateway_request_duration_seconds` | histogram | target, stream | To the end of the response or stream |
| `gateway_ttft_seconds` | histogram | target | Time to first token of the serving attempt (for debugging a provider) |
| `gateway_ttft_e2e_seconds` | histogram | — | Time to first token as the client saw it, from arrival, including failed attempts and fallback. **The SLO metric** |
| `gateway_tokens_total` | counter | target, kind | `prompt` (includes cached), `completion`, `cached` |
| `gateway_cost_usd_total` | counter | target | Spend |
| `gateway_fallbacks_total` | counter | alias, target | Served by a target other than the first |
| `gateway_upstream_attempts_total` | counter | target, outcome | Every upstream call: `ok`, `transient:503`, `skipped:open`, … |
| `gateway_rejected_total` | counter | reason | Rejected before routing: `unauthenticated`, `model_not_allowed`, `rate_limit`, `budget` |
| `gateway_circuit_state` | gauge | target | 0 closed, 1 half-open, 2 open (polled from Redis every 15 s) |
| `gateway_usage_log_dropped_total` | counter | — | Usage rows lost (queue full or Postgres down) |
| `gateway_event_loop_lag_seconds` | histogram | — | How late a 0.5 s timer fires; blocking code or CPU saturation |
| `gateway_auth_stale_served_total` | counter | — | Requests authenticated from a stale cached key during a Postgres outage |

## Grafana dashboard

Provisioned at startup. Open http://localhost:3000 → **LLM Gateway**. It's generated as code:
edit `config/grafana/build_dashboard.py`, run it, and commit both the script and the JSON.

| Row | Panels |
|-----|--------|
| Headline | Requests/s, fallback rate, error rate, p95 latency, spend this month, usage rows dropped |
| Latency | p95 latency per provider; p95 TTFT (client-side and per target) |
| Traffic | Requests/s by target and status; fallback rate by alias |
| Reliability | Circuit-breaker timeline; upstream attempts by outcome |
| Tokens and money | Tokens/min by target; spend/hour by target; rejections by reason |
| Per key (Postgres) | Spend per key this month (table, revoked keys marked); spend per key over time |

The per-key panels query Postgres directly. Per-key data never goes into Prometheus,
because of the cardinality rule above.

For docs screenshots, start the image renderer and render the dashboard:

```bash
docker compose --profile screenshots up -d renderer
curl -u admin:admin -o docs/img/dashboard.png \
  "localhost:3000/render/d/llm-gateway/llm-gateway?orgId=1&from=now-15m&to=now&width=1600&height=1800&kiosk=true&theme=dark"
```

## Usage log

One row per **admitted** request in `usage_log`:

| Column | Notes |
|--------|-------|
| `created_at`, `request_id` | `request_id` matches the logs and the `x-request-id` header |
| `key_id`, `key_prefix` | Who |
| `alias`, `target`, `provider`, `model` | What was asked for, and what served it |
| `status`, `error_code` | Outcome; `499` + `client_disconnected` for hang-ups |
| `streamed`, `fallback`, `attempts` | How it was served |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | From the provider's usage |
| `usage_estimated` | True when the provider sent no usage, so counts are estimates |
| `estimated_tokens` | The pre-call estimate, to check estimation quality |
| `cost_usd` | `null` for an unpriced model |
| `latency_ms`, `ttft_ms` | `ttft_ms` for streams |

Rows are written by a **background batch writer**. `submit()` never blocks or raises: if
Postgres is slow and the queue fills, rows are dropped and counted, rather than slowing chat
requests. On shutdown the queue is flushed.

Example report:

```sql
SELECT k.name, count(*) AS requests, sum(u.cost_usd) AS usd,
       round(100.0 * avg(u.fallback::int), 1) AS fallback_pct
FROM usage_log u LEFT JOIN api_keys k ON k.id = u.key_id
WHERE u.created_at >= date_trunc('month', now())
GROUP BY k.name ORDER BY usd DESC NULLS LAST;
```

## Logs

Structured JSON lines (structlog) on stdout, which suits Loki, CloudWatch or any log shipper.

- **Request ids:** each request gets a `request_id`. A valid incoming `x-request-id` is reused,
  and the id is echoed on every response.
- **Access log:** one line per HTTP request (method, path, status, ms). `/healthz`,
  `/readyz` and `/metrics` are skipped.
- **Usage line:** one per chat request, with tokens, cost and target.
- **Never logged:** prompt or completion content, API keys or auth headers. Prompt *size*
  is logged instead. Provider error text can contain key fragments, so it's kept out of
  client responses.

## SLOs and alerts

`config/prometheus-rules.yml`, loaded by Prometheus and validated with `promtool`:

| SLO (30 days) | Target |
|---------------|--------|
| Availability | 99.9% of admitted requests succeed. Gateway and upstream failures count, including streams that failed mid-way; client errors and hang-ups don't |
| Time to first token | p95 ≤ 1.5 s for streams (end to end, as clients see it) |

Alerts:

- **Error budget burn:** multi-window burn-rate alerts (the Google SRE workbook method).
  `GatewayErrorBudgetFastBurn` pages when the month's error budget would be gone in about
  2 days. `GatewayErrorBudgetSlowBurn` opens a ticket when it would be gone in about 5 days.
- **Other alerts:** `GatewayTTFTSLOBreach`, `GatewayDown`, `GatewayCircuitOpen`,
  `GatewayUsageLogDropping`, `GatewayEventLoopLag`.

Burn-rate alerting beats plain threshold alerts. A 2% error rate for five minutes is noise,
while 0.5% sustained for a day quietly spends the whole month's budget. Burn rate catches
both at the right urgency.
