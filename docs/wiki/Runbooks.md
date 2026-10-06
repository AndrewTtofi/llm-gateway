# Runbooks

One section per alert in `config/prometheus-rules.yml`. Every alert links here through its
`runbook_url`. Each section answers three questions: what it means, what to check first,
and how to fix it.

Commands assume the production compose stack (`docker-compose.prod.yml`). Run them on the
host as `docker compose -f docker-compose.prod.yml --env-file .env.prod …`, written
`dc …` below. Operator endpoints are on the localhost listener:
`curl -s localhost:8081/admin/providers -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"`.

Dashboards: Grafana → **LLM Gateway** (through the SSH tunnel to `127.0.0.1:3000`).

---

## GatewayErrorBudgetFastBurn

**Pages.** Requests are failing fast enough to spend 2% of the month's error budget per hour.
- **Check:** Grafana's error-rate panel by `status`.
  - **`503 all_providers_unavailable`:** every target in a chain is down or open. Check
    `/admin/providers`.
  - **`429:upstream_rate_limited`:** a provider is throttling the gateway's account.
  - **`502 upstream_error` / `504 upstream_timeout`:** a provider is failing. Check its
    status page.
  - **`200:<code>`:** streams failing mid-way.
- **Fix:**
  - **Provider down:** fallback should already be absorbing it. If every target in a chain
    is down, add another provider to the chain in `config/models.yaml` and reload.
  - **Throttled:** raise the provider account's rate limits, or spread traffic over more
    targets.

## GatewayErrorBudgetSlowBurn

**Ticket.** A low but steady error rate that would use the month's budget in about 5 days.
- **Check:** the same panels as the fast burn, over 6 hours. Look for one target or one
  alias standing out.
- **Fix:** usually a flaky target. Move it later in its chains, or look into its errors in
  the logs (`dc logs gateway | grep upstream`).

## GatewayTTFTSLOBreach

**Ticket.** p95 time to first token has been above 1.5 s for 10 minutes.
- **Check:** whether it's one target (`gateway_ttft_seconds` per target) or all of them.
  Also check the gateway's own `server-timing: admit` and event-loop lag.
- **Fix:** route latency-sensitive aliases to faster models (`optimize: latency` on policy
  aliases). If the gateway itself is the cause, see
  [GatewayEventLoopLag](#gatewayeventlooplag).

## GatewayDown

**Pages.** Prometheus can't scrape a replica.
- **Check:** `dc ps`, then `dc logs --tail 100 gateway`. Is it crash-looping? A bad config
  fails at startup with the validation error in the log.
- **Fix:** fix the config, or roll back `GATEWAY_VERSION` in `.env.prod` and run
  `dc up -d`. The other replica and Caddy's health checks keep traffic flowing meanwhile.

## GatewayCircuitOpen

**Ticket.** A target's breaker has been open for 5 minutes. Traffic is going to its fallbacks.
- **Check:**
  - `/admin/providers` shows the state.
  - Breaker alerts in the webhook carry the reason (`quota exhausted`,
    `authentication failed`).
  - The provider's status page.
- **Fix:**
  - **Quota or auth:** top up the account, or rotate the key and restart.
  - **Outage:** wait. Background probes close the breaker when the provider answers again.

## GatewayUsageLogDropping

**Ticket.** Usage rows aren't being written: the billing record has gaps.
- **Check:**
  - `dc logs gateway | grep "usage"` for the cause.
  - Postgres health: `dc ps postgres`, and `dc exec postgres pg_isready`.
  - Disk space.
- **Fix:** restore Postgres. Rows queued in memory are written when it's back (the queue
  holds 10 000). Rows dropped beyond that are lost. Spend in Redis is unaffected, so
  budgets stay right.

## GatewayEventLoopLag

**Ticket.** A replica's event loop is late by over 100 ms at p99: its CPU is saturated, or
something blocks it.
- **Check:**
  - CPU per container: `docker stats`.
  - Concurrent streams on the dashboard.
  - Very large prompts: requests over 20 000 characters are scanned in a worker thread,
    but very many of them still cost CPU.
- **Fix:** add replicas (`deploy.replicas`). One replica uses one core and stays close to
  direct-path latency up to about 100 concurrent streams
  ([Testing and benchmarks](Testing-and-Benchmarks.md)).

## GatewayRedisFailingOpen

**Pages.** The gateway can't reach Redis, so it's running without it:
- rate limits allow everything;
- breakers read as closed;
- budgets use each replica's last known spend, and spend is queued to be written later.
- **Check:** `dc ps redis`, `dc logs --tail 50 redis`, and Redis's memory and disk.
- **Fix:** restore Redis. The queued spend is written when it's back. Its data (buckets,
  spend, breakers) persists in the `redis-data` volume (append-only). If it was lost,
  month-to-date spend restarts from 0. Rebuild it from `usage_log` with
  [Rebuilding spend](#rebuilding-spend-after-losing-redis) if budgets matter this month.

## GatewayAuthServedStale

**Ticket.** Postgres (the key store) is down, and keys seen in the last 10 minutes are
served from cache. New or unseen keys get `503 auth_unavailable`.
- **Check:** Postgres, as for [GatewayUsageLogDropping](#gatewayusagelogdropping).
- **Fix:** restore Postgres. To recover data, see [Restore](#restore-from-a-backup).

## GatewayInjectionSpike

**Ticket.** More than 50 prompt-injection detections in 15 minutes.
- **Check:** which rules fired (`gateway_guardrail_detections_total` by `rule`), and which
  keys (usage-log rows around the time, or the warning lines in the logs; rule names only,
  never content).
- **Fix:** one key probing: revoke or block it (`injection: block` on its tier). A rule
  misfiring on normal traffic: lower its weight in `config/guardrails.yaml` and reload.

## GatewayConcurrencyRejections

**Ticket.** Keys keep hitting their limit on requests in flight
(`429 concurrency_limit_exceeded`).
- **Check:** which keys (`usage_log`, or the logs). Is it one runaway client, or a real
  need?
- **Fix:** a runaway client: contact its owner, or revoke the key. A real need: move the key
  to a tier with a higher `concurrent_requests`.

---

## Backups

The `backup` service in `docker-compose.prod.yml` runs `pg_dump` (custom format) every day,
keeps the last `BACKUP_KEEP_DAYS` (default 14) in the `pg-backups` volume, and writes
`last-success` after each good dump. Copy the volume off the host, too. A backup on the same
disk doesn't survive losing the disk.

```bash
dc exec backup ls -l /backups                       # dumps, newest last
docker run --rm -v llm-gateway_pg-backups:/b alpine tar -C /b -cf - . > backups.tar   # copy off-host
```

## Restore from a backup

```bash
dc stop gateway prune-usage                         # no writes while restoring
dc exec backup sh /restore.sh /backups/<file>.dump  # drops and recreates the gateway database
dc up -d                                            # migrations run, replicas start
```

`restore.sh` checks that the dump is readable before it touches the database, and prints
row counts for `api_keys` and `usage_log` afterwards. Run a **restore drill** now and then:
`make restore-drill` restores the latest dump into a scratch database and compares row
counts, without touching the live one.

## Rebuilding spend after losing Redis

Month-to-date spend lives in Redis. To rebuild it from the usage log for the current month:

```sql
SELECT key_id, team, sum(cost_usd) FROM usage_log
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
GROUP BY key_id, team;
```

Then set `spend:{<key_id>}:<YYYY-MM>` (and `spend:{team:<name>}:<YYYY-MM>` per team) in
Redis to those sums.
