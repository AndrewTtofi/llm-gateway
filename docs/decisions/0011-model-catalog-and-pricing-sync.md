# 0011 — Model catalog and reviewed price sync

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 8

## Context
Apps built on the gateway want to pick a model by what it costs and how well it fits:
cheapest for bulk work, best quality for hard tasks, tools or vision when they need them.
Two things were missing:

1. **Current prices.** `pricing.yaml` was maintained by hand, and prices change. No provider
   publishes prices through an API. The Anthropic and OpenAI model APIs return IDs, not
   prices.
2. **A way for apps to see the options.** `/v1/models` lists names and chains only. There
   was nothing on price, context window, capabilities, quality, or how a model is actually
   performing right now.

Prices drive budgets and spend reports (ADR 0007), so a wrong price is a wrong invoice.

## Options considered
**Price source:**

1. **Hand-maintained YAML only.** It's auditable, but it goes stale. Phase 7's review found
   OpenAI's cached-input prices had been missing all along.
2. **Automatic runtime refresh from a public catalog.** Always current, but it trusts a third
   party with the numbers that bill your teams, and changes happen silently.
3. **Sync tool plus review.** A script compares config with public catalogs and proposes a
   diff. A human applies and commits it, and a weekly job reports drift. Current within a
   week, with every change visible in git.

**Public catalogs:**

- **LiteLLM's community price list:** keyed by the providers' own model IDs, and includes
  context windows and capability flags. Community-maintained, updated often.
- **OpenRouter's `/models` API:** an official API, but with its own IDs and only OpenRouter's
  prices (which normally match list prices).

Using two independent sources and comparing them catches errors in either.

**Live performance:** read Prometheus from the gateway (a new dependency on the request
path) or keep a small in-process window. The second is simpler and always available, but
each replica only sees its own traffic.

## Decision
- **Sync tool plus review (option 3).** `tools/sync_prices.py` (`make prices`):
  - LiteLLM is the primary source and OpenRouter the cross-check.
  - It prints a diff and exits 1 on drift. `--write` applies only the changes the two sources
    agree on (within 0.5%). Disputed prices need `--force`, after checking the provider's
    pricing page.
  - Fake and `dev_only` providers are never looked up. Local models with no public source are
    reported and left alone.
  - A weekly workflow (`.github/workflows/prices.yml`) runs the check and opens or updates one
    issue on drift. It has read-only repo access and never edits config.
- **`config/catalog.yaml`** holds per-target facts:
  - `context_window`, `max_output_tokens` and `capabilities` (tools, vision, reasoning,
    json_schema) are synced.
  - `quality` (1–5) is the operator's own score, which the sync never changes. Public quality
    rankings don't know your use case.
- **`GET /v1/catalog`:**
  - For the calling key, it lists the aliases and targets it may use, with price per 1M
    tokens, a 3:1 blended price, the catalog facts, breaker state, and live stats.
  - The live stats are this replica's last 15 minutes: requests, attempt error rate, p50/p95
    latency and TTFT for the serving attempt.
  - Filters: `capability=` (repeatable) and `min_context=`. Sorting: `price`, `quality`,
    `ttft`, `latency`, `name`.
  - It counts as one request against the key's rate limit, because each call reads breaker
    state from Redis.

## Consequences
- Apps can choose a model, or an alias, from data instead of guesswork.
- Price drift is noticed within a week and fixed through a normal reviewed PR.
- Live stats are per replica and since that replica's start. Behind a load balancer, two
  calls may show different numbers. For fleet-wide history, use Prometheus and Grafana.
  Latency depends on answer length, so TTFT is the fairer comparison between models.
- Long-context price tiers (e.g. OpenAI above 272K input tokens) and batch prices aren't
  modelled. The gateway bills the standard short-context rate.
- **Next step: policy routing.** Clients ask for `auto` with hints (optimise for cost,
  latency or quality; required capabilities; minimum quality). The gateway then builds the
  chain from this catalog per request. The catalog is designed to feed that.
