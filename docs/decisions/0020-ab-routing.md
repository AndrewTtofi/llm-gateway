# 0020 — A/B routing with weighted, sticky variants

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
Changing a model or a prompt for everyone at once is a leap of faith. A/B routing sends a
share of traffic to the change and compares the arms on latency, errors, cost and, with
the judge (ADR 0022), quality.

## Options considered
1. **Feature flags in each app.** Every app re-implements splitting and measurement.
2. **Weighted variants of an alias in the gateway,** measured where all the data already
   is.

## Decision
Option 2 (`app/routing/ab.py`). An alias may define `variants` (name, weight, chain,
optional `system_prefix`) and `sticky`:

- **Assignment:**
  - With `key` (default) or `user` (the request's `user` field), the arm is a hash of the
    alias and that value mapped onto the weights. A caller stays on one arm, and aliases
    split independently.
  - `request` picks at random every time.
  - `x-gateway-variant: <name>` pins an arm only on aliases with `allow_pin: true` (QA),
    so callers can't choose a pricier arm or skew the split.
- **The arm** supplies the fallback chain. Its `system_prefix` goes in front of the system
  prompt: that's how prompt variants are tested.
- **Recorded:** the `x-gateway-variant` header, `usage_log.variant` (migration 0005),
  `gateway_variant_requests_total` / `_duration_seconds` / `_cost_usd_total`, and Grafana
  panels comparing arms. Variant names are validated short slugs, so labels stay bounded.
- **Cache:** each arm has its own response-cache entries.

## Consequences
- **Model and prompt changes can be rolled out gradually** and measured on real traffic.
- **Sticky assignment per key** means an app with one key is entirely in one arm. Use
  `sticky: user` with the request's `user` field to split an app's end users.
- **No significance testing:** the dashboard shows the numbers; deciding whether a
  difference is real is up to you.
