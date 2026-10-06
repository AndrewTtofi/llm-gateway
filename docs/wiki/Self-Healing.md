# Self-healing and alerts

ADR 0019. On top of the circuit breakers ([Routing and reliability](Routing-and-Reliability.md#circuit-breakers)),
the gateway recovers providers in the background and tells you when they fail.

```yaml
# config/models.yaml
self_healing:
  probes: true
  probe_interval_seconds: 10
  probe_max_tokens: 16
  quarantine_seconds: 600
  quarantine_status: [401]
  alert_webhook_env: ALERT_WEBHOOK_URL
  alert_min_interval_seconds: 60
```

## Background probes

When a breaker's open period ends, it goes half-open and waits for one probe request. Without
probes, that probe is a real user's request, which fails if the provider is still down. With
probes, a background task sends a tiny request (`probe_max_tokens` output tokens) instead:

| Probe result | Breaker |
|--------------|---------|
| Answer | Closed; traffic returns |
| Provider error, or no answer within `probe_timeout_seconds` | Open again |
| Client-fault answer (e.g. 400) | Closed: the provider is up and answering |
| Provider has no API key | Skipped |

The breaker's probe token is atomic in Redis, so only one replica probes a target at a time.
Cost is at most one tiny request per target per open period, billed to your provider
account. `gateway_probes_total{target,result}` counts the probes.

## Quarantine

Some failures don't fix themselves in 30 seconds and are about the provider account, not
the request:
- a revoked or wrong API key (401);
- exhausted credit.

403 and 404 aren't on the list by default. They can be specific to one request (a feature
not enabled, a resource id), and one request shouldn't take a model away from every tenant.
They go through the normal breaker threshold.

These **quarantine** the target: the breaker stays open for `quarantine_seconds` (default
10 minutes) instead of `open_seconds`, and records the reason. Traffic goes to the next
model in the chain meanwhile. Fix the key or credit, and the next probe after the
quarantine brings it back. To bring it back sooner, restart or reload.

## Alerts

Set `ALERT_WEBHOOK_URL` to an incoming-webhook URL and every breaker state change is posted.
Slack accepts the payload as-is; other tools get the same JSON.

```json
{"text": "🔴 LLM gateway: openai/gpt-6-astra circuit closed → open (quota exhausted)",
 "target": "openai/gpt-6-astra", "from": "closed", "to": "open", "reason": "quota exhausted"}
```

- **Deduplication:** each replica watches breaker states, but a Redis lock per target and
  state lets **one** alert through per `alert_min_interval_seconds` across the fleet.
- **Startup:** at startup the current states are the baseline, so a restart doesn't alert.
- **Half-open:** moves into half-open aren't alerted on their own. You hear "open" and then
  "closed" (or nothing more, while it stays down).
- **Delivery:** alerts are sent in the background, so a slow webhook never delays polling.
- **Privacy:** alerts never include request content.
- **Metric:** `gateway_alerts_total{result}` counts sent and failed posts.

The same webhook also carries **budget alerts**, when a key or team reaches 50 / 80 / 100%
of its monthly budget ([Keys, limits and budgets](Keys-Limits-and-Budgets.md#budget-alerts)).

Prometheus's `GatewayCircuitOpen` alert ([Observability](Observability.md#slos-and-alerts))
stays as the backstop.
