# 0019 — Self-healing: background probes, quarantine, alerts

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
A breaker that has been open for `open_seconds` lets one real request through as a probe
(ADR 0004). If the provider is still down, a user pays for the test. Faults like a revoked
key or exhausted credit don't heal in 30 seconds, so a 30-second cycle just keeps failing.
And nobody was told when a provider went down.

## Options considered
1. **Health-check endpoints per provider.** Most providers have none that reflects a
   model's real health.
2. **Synthetic probes through the normal adapter,** using the breaker's existing probe
   token.
3. **Leave it to traffic and Prometheus alerts.** Simple, but users pay for the probes,
   and the alerts are only as good as someone's routing rules.

## Decision
Option 2, plus quarantine and webhooks (`app/routing/selfheal.py`, config
`self_healing`):

- **Probes:**
  - Every `probe_interval_seconds`, each half-open target in a chain gets a tiny request
    (`probe_max_tokens`, default 16, the Responses API minimum).
  - The breaker's atomic probe token means one replica probes a target at a time.
  - Success closes the breaker; a provider error reopens it; a client-fault answer counts
    as healthy (it answered).
  - Unconfigured providers are skipped.
- **Quarantine:** an auth failure (401) or exhausted quota holds the breaker open for
  `quarantine_seconds` (default 600), with a reason. This happens both on user traffic and
  on probes. 403 and 404 are left to the normal breaker by default, because they can be
  specific to one request and quarantine affects every tenant.
- **Probe timeouts:** a probe that times out counts as a failure.
- **Half-open:** half-open transitions aren't alerted on their own.
- **Delivery:** alerts are sent in background tasks.
- **Alerts:**
  - Breaker state changes (closed, open, half-open), with the quarantine reason, are posted
    to a Slack-compatible webhook. The URL comes from `$ALERT_WEBHOOK_URL`; unset means off.
  - Each replica watches its poll of breaker states. A Redis `SET NX` per target and state
    lets one alert through per `alert_min_interval_seconds` fleet-wide.
  - No request content ever goes into an alert.

## Consequences
- **Recovery is detected without spending users' requests,** and people find out within
  a poll interval.
- **Probe cost** goes to the provider account: at most one tiny request per target per
  open period. Turn probes off with `probes: false`.
- **Alert loss:** the in-memory "last seen state" is per replica. A restart re-baselines
  without alerting, so a change during a restart can be missed. Prometheus's
  `GatewayCircuitOpen` alert remains the backstop.
