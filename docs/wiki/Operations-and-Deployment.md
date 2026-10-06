# Operations and deployment

For a ready-made single-host setup (TLS, two replicas, migrations, retention and
monitoring), see [Production deployment](Production-Deployment.md). This page covers what any
deployment needs.

## Shape of a deployment

```
            ┌──────────── load balancer (TLS, long idle timeouts, no SSE buffering) ─────┐
 clients ──►│  gateway replica 1  ·  gateway replica 2  ·  …  (stateless containers)     │
            └───────┬─────────────────────┬─────────────────────┬────────────────────────┘
                    │                     │                     │ :9100 (internal)
               managed Redis        managed Postgres        Prometheus ──► Grafana / alerts
```

- **Gateway:** the `Dockerfile` image. It's stateless, so scale horizontally. One replica
  (one core) handled about 200 concurrent streams within 10% of a direct connection, and 2
  replicas reached 614 req/s on a laptop. Plan from your own load test, not these numbers.
- **Redis:** must be shared by all replicas, because buckets, spend and breakers live there.
  Run it highly available if you need strict limit enforcement. Without it, limits fail open.
- **Postgres:** keys and the usage log. The usage log grows by one row per request, so plan
  retention (partition by month, or delete old rows).
- **Prometheus/Grafana:** use your existing stack. Scrape every replica's `:9100`, and load
  `config/prometheus-rules.yml` and the dashboard JSON.

## Health checks

| Probe | Endpoint | Behaviour |
|-------|----------|-----------|
| Liveness | `/healthz` | 200 while the event loop answers |
| Readiness | `/readyz` | 503 `starting` until startup completes; then 200 `ready` or `degraded` |

`/readyz` **does not fail when Redis or Postgres is down**. The body reports them
(`{"redis":"ok","postgres":"unreachable"}`), but the status stays 200.

The reason: every replica shares those dependencies. If readiness failed on them, one Redis
blip would mark every replica not-ready at the same moment, and the load balancer would drop
all of them. A gateway that can still serve (limits fail open, cached keys keep working)
would become a total outage. Readiness should reflect the instance's **own** state.

The dependency status comes from a background check every 5 s (concurrent, 0.5 s timeout), so
a probe never waits on a hung database. Alert on dependency health separately, through
metrics and logs.

## Migrations

Schema changes use Alembic (`migrations/`).

- **Single instance:** the image's default command runs `alembic upgrade head`, then starts
  uvicorn. That's fine here.
- **Several replicas:** run migrations **once per deploy**, as a job or init step, and start
  replicas without them. Otherwise every replica races to migrate:

```bash
alembic upgrade head                      # the migration job
uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 30   # replicas
```

`docker-compose.bench.yml` shows this pattern with a one-shot `migrate` service.

## Load balancer and proxies

- **Timeouts:** idle and request timeouts must exceed the longest stream (`stream_total`,
  900 s by default). The usual 60 s default cuts long answers off.
- **No buffering:** turn off response buffering for `text/event-stream`. The gateway sends
  `x-accel-buffering: no` for nginx, but other proxies need configuring.
- **Health checks:** point them at `/readyz`.
- **Request ids:** pass `x-request-id` through, so one id follows a request across systems.

## Shutdown and rollouts

On `SIGTERM`, uvicorn stops accepting connections and waits up to
`--timeout-graceful-shutdown` (30 s in the image) for in-flight requests and streams. The
gateway then flushes queued usage rows and closes its pools. Set the orchestrator's grace
period above that (Kubernetes: `terminationGracePeriodSeconds: 45`). In testing, 90/90 open
streams completed through a SIGTERM.

Streams longer than the grace period are cut. For very long generations, roll out slowly,
or raise both timeouts.

## Configuration changes

- **Models, aliases, prices, limits:** edit YAML and call `POST /admin/reload` on **each**
  replica, or send `SIGHUP`, or redeploy. In Kubernetes, the simplest consistent option is
  to mount the config as a ConfigMap and roll the deployment.
- **Secrets:** environment variables from your secret store. Never bake them into the image.

## Exposure

| Surface | Expose to |
|---------|-----------|
| `:8000 /v1/*` | Your apps (internal network, or public behind TLS) |
| `:8000 /admin/*` | Operators only; restrict at the network or ingress level as well as with the admin key |
| `:8000 /readyz`, `/healthz` | The load balancer; `/readyz` names your dependencies |
| `:9100 /metrics` | Prometheus only |

## Where to deploy

Any container platform works: Cloud Run, ECS/Fargate, Kubernetes, Fly.io or a VM with
Compose. Things to check:

- **Streaming support:** Cloud Run and ALB support streaming responses, but check their
  request timeouts (Cloud Run: up to 60 min; ALB idle timeout: raise it from 60 s).
- **CPU throttling:** Cloud Run throttles CPU outside requests by default. That's fine for
  the request path, but the background tasks (usage writer, breaker poller, dependency
  check) need "CPU always allocated" to run reliably.
- **Managed Redis and Postgres:** Memorystore / ElastiCache, Cloud SQL / RDS.

## Runbook

| Symptom | Check |
|---------|-------|
| Fallback rate jumped | "Upstream attempts by outcome": which target and which error? Is its breaker open? Is there a provider status-page incident? |
| 503 `all_providers_unavailable` | Every target in the chain is open or failing. Add a target from another provider to the chain |
| 503 `upstream_quota_exhausted` | The provider account is out of credit. Top it up. Fallback hides this, so alert on it |
| Many 429 `rate_limit_exceeded` for one app | That key's limits; `max_tokens` estimates; raise `tokens_per_minute` |
| `gateway_usage_log_dropped_total` rising | Postgres is slow or down; the dashboard spend panels will undercount |
| `gateway_event_loop_lag_seconds` high | CPU saturation or a blocking call; add replicas |
| A target stays open for 10 minutes | Quarantined: bad key, unknown model or no credit. Check the alert or the `/admin/providers` reason, fix it, then restart or wait for the probe |
| `gateway_guardrail_detections_total` jumps | An attack, or a rule that's too broad. Check which rule, then tune `guardrails.yaml` |
| `gateway_judge_total{result="dropped"}` rising | The judge can't keep up. Lower `sample_rate` or use a faster judge alias |
| `usage_log` growing too large | Check the retention job (`prune-usage`) and `USAGE_RETENTION_DAYS` |
| Spend shows `null` cost | A target is missing from `pricing.yaml` |
