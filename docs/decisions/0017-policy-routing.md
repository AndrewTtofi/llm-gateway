# 0017 — Policy routing (`model: auto`)

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
Apps had to pick an alias, and so a fixed chain. The catalog (ADR 0011) knows each
model's price, capabilities, context window, quality score and live latency, so the gateway
can choose per request instead: the cheapest model that can do *this* request, or the best,
or the fastest.

## Options considered
1. **Let apps choose from `/v1/catalog`.** Already possible, but every app re-implements
   the logic, and the choice goes stale as breakers open and prices change.
2. **Route by classifying the prompt's difficulty with a model.** Adds latency and cost to
   every request, and is hard to predict.
3. **Policy aliases:** constraints and an objective in config, evaluated per request from
   data the gateway already has.

## Decision
Option 3 (`app/routing/policy.py`). An alias has a `policy` instead of a `chain`:
`optimize` (`cost`, `quality` or `latency`), `candidates` (empty means every known
non-test model), `needs`, `min_quality`, `max_blended_price` and `max_chain`.

Per request:
1. **Constraints.** Candidates are filtered by capability (the policy's `needs` plus what
   the request implies: tools, image parts, a JSON schema), by context window against the
   estimated prompt plus `max_tokens`, and by quality and price. If none passes: 400
   `no_route`.
2. **Availability.** Then by availability: provider key present, breaker not open. If
   none is left: 503 `no_route` (retryable).
3. **Ranking.**
   - `cost`: blended price, then quality.
   - `quality`: score, then price.
   - `latency`: this replica's measured p50 TTFT (or latency), then price.

   The first `max_chain` become the request's chain, with the usual retries, fallback and
   breakers.

**Client hints:** a `route` object in the body or the `x-gateway-route` header may
**tighten** a policy:
- a different `optimize`;
- more `needs`;
- a higher `min_quality`;
- a lower `max_blended_price`.

Hints never widen the candidate list. Unknown hints are a 400. `route` is an extension field
and never reaches a provider. Authorisation is on the alias name: a key allowed `auto` may
use its candidates. The response carries `x-gateway-route: optimize=…; considered=…; chain=…`.

A default `auto` alias is shipped: cost-optimised, `min_quality: 3`.

## Consequences
- **Apps describe what they need, not which model.** New models join the pool through
  config alone.
- **Quality comes from your scores** in `catalog.yaml`, so the ranking reflects your own
  evaluations rather than public benchmarks.
- **Latency ranking is per replica** and needs traffic. Unmeasured models rank by price
  after the measured ones.
- **Reservations follow the chain:** the budget reservation uses the chain's first model.
  A cost policy reserves less, a quality policy more.
