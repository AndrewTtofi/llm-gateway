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
| `gateway_upstream_attempts_total` | counter | target, outcome | Every upstream call: `ok`, `transient:503`, `skipped:open`, `skipped:busy` (no free connection), … |
| `gateway_rejected_total` | counter | reason | Rejected before routing: `unauthenticated`, `model_not_allowed`, `rate_limit`, `concurrency`, `budget`, `team_budget` |
| `gateway_redis_fail_open_total` | counter | what | Calls answered without Redis (`rate limiter`, `budget tracker`) during an outage |
| `gateway_circuit_state` | gauge | target | 0 closed, 1 half-open, 2 open (polled from Redis every 15 s) |
| `gateway_usage_log_dropped_total` | counter | — | Usage rows lost (queue full or Postgres down) |
| `gateway_event_loop_lag_seconds` | histogram | — | How late a 0.5 s timer fires; blocking code or CPU saturation |
| `gateway_auth_stale_served_total` | counter | — | Requests authenticated from a stale cached key during a Postgres outage |
| `gateway_cache_total` | counter | mode, result | Response cache: `hit_exact`, `hit_semantic`, `miss`, `store`, `bypass`, `refresh` |
| `gateway_variant_requests_total` | counter | alias, variant, status | Requests per A/B arm |
| `gateway_variant_duration_seconds` | histogram | alias, variant | Latency per A/B arm |
| `gateway_variant_cost_usd_total` | counter | alias, variant | Spend per A/B arm |
| `gateway_guardrail_detections_total` | counter | rule, action | Prompt-injection detections by rule (`classifier` for classifier verdicts, `unscanned` for requests with text over the scan budget) |
| `gateway_judge_score` | histogram | alias, variant | LLM-as-judge scores (1–5) |
| `gateway_judge_total` | counter | alias, result | Judge samples: `scored`, `dropped`, `error`, `unparsable` |
| `gateway_probes_total` | counter | target, result | Background probes: `recovered`, `failed`, `skipped`, `busy` |
| `gateway_alerts_total` | counter | result | Alert webhook posts: `sent`, `failed` |
| `gateway_budget_alerts_total` | counter | level | Keys or teams reaching 50 / 80 / 100% of a monthly budget |

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
| Per team (Postgres) | Spend per team this month; spend per team over time |
| A/B tests | Requests/s, error rate and p95 latency per arm; cost per request per arm (Postgres) |
| Quality | Average judge score per alias and arm; judge labels this month (Postgres) |

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
| `created_at`, `request_id` | `request_id` matches the logs and the `x-request-id` header; always the gateway's own |
| `client_request_id` | The caller's own id, if it sent one (`x-client-request-id` or `x-request-id`) |
| `key_id`, `key_prefix` | Who |
| `alias`, `target`, `provider`, `model` | What was asked for, and what served it |
| `status`, `error_code` | Outcome; `499` + `client_disconnected` for hang-ups |
| `streamed`, `fallback`, `attempts` | How it was served |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | From the provider's usage (64-bit) |
| `usage_estimated` | True when the provider sent no usage, so counts are estimates (a cut-off request, or timed-out attempts) |
| `estimated_tokens` | The pre-call estimate, to check estimation quality |
| `cost_usd` | `null` for an unpriced model; 0 for cache hits (`target` = `cache/exact` or `cache/semantic`) |
| `team`, `variant` | The key's team and the A/B arm, at request time |

The gateway's own calls get rows too, so spend in the usage log matches the provider's
invoice (ADR 0023):

| `alias` | Call | Charged to |
|---------|------|------------|
| `_classifier` | Prompt-injection classifier | The key whose request was checked (its spend and budget) |
| `_embedding` | Semantic-cache embedding | The key whose request was looked up |
| `_judge` | LLM-as-judge grading | The operator (`key_prefix` `_internal`), joinable by `request_id` |
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

- **Request ids:** each request gets its own `request_id`, echoed as `x-request-id`. A
  caller's id, from `x-client-request-id` or `x-request-id`, is kept beside it as
  `client_request_id`, echoed as `x-client-request-id`, and is never used *as* the request
  id. So one tenant can't make its rows share an id with another's (ADR 0023).
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
| Availability | 99.9% of admitted requests succeed. Gateway and upstream failures count, including streams that failed mid-way and 429s passed on from a provider (`429:upstream_rate_limited`: a quota outage pages). Client errors, including the key's own limits, and hang-ups don't |
| Time to first token | p95 ≤ 1.5 s for streams (end to end, as clients see it) |

Alerts:

- **Error budget burn:** multi-window burn-rate alerts (the Google SRE workbook method).
  `GatewayErrorBudgetFastBurn` pages when the month's error budget would be gone in about
  2 days. `GatewayErrorBudgetSlowBurn` opens a ticket when it would be gone in about 5 days.
- **Other alerts:** `GatewayTTFTSLOBreach`, `GatewayDown`, `GatewayCircuitOpen`,
  `GatewayUsageLogDropping`, `GatewayEventLoopLag`.
- **Safety alerts (ADR 0023):**
  - `GatewayRedisFailingOpen` (page): limits and budgets are running without Redis.
  - `GatewayAuthServedStale`: keys are checked from cache because Postgres is down.
  - `GatewayInjectionSpike`: more than 50 detections in 15 min.
  - `GatewayConcurrencyRejections`: a key keeps hitting its in-flight limit.

Every alert has a `runbook_url` to its section in [Runbooks](Runbooks.md). In production,
Alertmanager routes them to a Slack-compatible webhook (`ALERTMANAGER_SLACK_URL`).

Burn-rate alerting beats plain threshold alerts. A 2% error rate for five minutes is noise,
while 0.5% sustained for a day quietly spends the whole month's budget. Burn rate catches
both at the right urgency.
