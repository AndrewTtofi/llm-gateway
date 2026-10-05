# 0004 — Retries, fallback chains and circuit breakers

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 3

## Context
An alias is an ordered chain of `provider/model` targets. When a target fails, the
gateway has three tools: try it again (retry), try the next target (fallback), or stop
sending it traffic for a while (circuit breaker). Applied blindly they make things
worse: retrying a bad request wastes money, retrying an overloaded provider adds load,
and falling back on a client error just collects the same 400 from every provider.

## Decision

**Classify every failure first** (`app/routing/router.py::classify`):

| Failure | Retry same target | Fall back | Counts against breaker |
|---|---|---|---|
| Client fault: 400/413/422, untranslatable request | no | no — return it | no |
| Gateway fault: 401/403/404, quota exhausted, missing API key | no | yes | yes |
| Provider type not supported / not configured | — | yes | no (never called) |
| Transient: `retry_on_status` (408/409/429/5xx/529), timeouts, connection errors | yes, with backoff | yes, after retries | yes |
| Untranslatable for *this* provider (e.g. `n>1` on Anthropic) | no | yes | no |

**Retries:** up to `retry.max_attempts_per_provider` per target, exponential backoff
with full jitter (`random(0, min(max, base·2^n))`) so a burst of clients doesn't retry
in lockstep. A `retry-after` the provider sends is honoured (plus ≤10% jitter) if it's
within `backoff_max_ms`; a longer one skips straight to the next target.

**Fallback:** next target in the chain. The response says who served it:
`x-gateway-provider`, `x-gateway-attempts` (upstream calls actually made),
`x-gateway-fallback`. If nothing works, the error is ranked: a provider failure (the
latest one) beats "this provider can't express the request"; if nothing failed but a
target was skipped because its breaker is open, a retryable 503
`all_providers_unavailable` — never a 400 for a request that would succeed later.

**Circuit breaker per target (`provider/model`), not per provider.** Anthropic signals
overload per model (529); one busy Opus shouldn't push Haiku traffic away. Provider-wide
outages trip each target separately — a few extra failed calls, acceptably cheap.

States: **closed** → `failure_threshold` failures within `window_seconds` → **open**
(target skipped) for `open_seconds` → **half-open**: one probe request is let through
(atomic `SET NX` with a unique token). **Only the probe decides**: its success closes
the breaker, its failure re-opens it. Stragglers that started before the trip can't
close or extend it. A probe that ends without a verdict (client hung up, request
untranslatable) releases its slot; a client-fault answer counts as success (the
provider responded). `probe_timeout_seconds` must exceed the slowest call, or a slow
but healthy probe lets a second probe through. A tripped breaker that's never probed
forgets itself after open + probe timeout + window, so no state is immortal.

Direct `provider/model` requests are limited to known targets (alias chains and
per-provider model lists): otherwise any client could mint unbounded breaker state.

**State in Redis** so every gateway instance shares it, updated with Lua scripts so
"count failure and maybe open" is atomic across instances. Keys share a `{hash tag}`
so the scripts work on Redis Cluster. If Redis is unreachable the breaker **fails
open** (allows traffic), logs once, and skips Redis for 5 s — so an outage costs one
`redis_timeout_ms` (100 ms), not a timeout on every request. Successes outside a probe
make no Redis call at all. An in-memory store with identical semantics is used for single-process dev
and in tests; the shared test suite runs against both.

## Consequences
- Worst-case latency grows with chain length × attempts × timeouts; timeouts per
  provider are the control. Revisit with a total routing deadline if Phase 6 shows it.
- Breakers only learn from real traffic; there are no background health checks yet.
- Streams can only be retried/fallen back before the first chunk — see ADR 0005.
