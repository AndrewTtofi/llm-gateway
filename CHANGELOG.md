# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

Version plan: each completed phase bumps the minor version
(Phase 1 → 0.1.0, Phase 2 → 0.2.0 … Phase 7 → 1.0.0).

## [Unreleased]

### Added
- `GET /v1/catalog`: price, a blended price, context window, capabilities, quality score, breaker state and live per-model stats for the calling key's models. Live stats are latency, TTFT and error rate over this replica's last 15 minutes. Supports filters and sorting (ADR 0011)
- `config/catalog.yaml`: model facts plus the operator's quality score
- `make prices` (`tools/sync_prices.py`): compares pricing and catalog with LiteLLM's price list and OpenRouter's API, and prints a reviewed diff. `--write` applies only the changes both sources agree on
- Weekly `prices` workflow: opens or updates one issue when prices drift
- Wiki page "Choosing models"

### Fixed
- OpenAI cached-input prices were missing from `pricing.yaml`, so cache reads were billed at the full input price

### Added
- Wiki (`docs/wiki/`, published to the GitHub Wiki tab with `scripts/publish_wiki.sh`):
  - getting started, core concepts, architecture;
  - providers and translation, routing and reliability, keys/limits/budgets;
  - observability, configuration and API references;
  - operations, testing and benchmarks, security;
  - a multi-app use case, subscriptions and provider terms, FAQ, glossary.

## [1.1.0] - 2026-10-05 (Phase 8 — Anthropic Messages API)

### Added
- `POST /v1/messages`: the Anthropic Messages API, for the Anthropic SDKs and Claude Code. It's translated at the edge into the internal format, so fallback, limits, budgets and metering all apply, and it can fall back to non-Anthropic models. Responses, stream events and errors come back in Anthropic's format (ADR 0010)
- `POST /v1/messages/count_tokens` (the gateway's estimate)
- `x-api-key` accepted as an alternative to `Authorization: Bearer`
- Streams can be written in more than one wire format (`StreamFormat`; OpenAI by default)
- `/v1/messages` streams keep tool-call blocks open until the calls end (interleaved arguments stay correct). Rejections log a field name only. `count_tokens` is rate-limited. Budget 429s have type `billing_error`. `metadata.user_id` is hashed. Refusals are mapped

## [1.0.0] - 2026-10-05 (Phase 7 — ship it)

### Added
- `/readyz` readiness endpoint: `503` until startup completes, then `200` with Redis/Postgres status (`ready` / `degraded`). Shared dependencies don't fail readiness, which avoids pulling every replica at once
- Grafana image renderer (compose profile `screenshots`) for reproducible dashboard screenshots; `docs/img/dashboard.png`
- `docs/WRITEUP.md`: project write-up and a LinkedIn draft

### Changed
- README rewritten: what "OpenAI-compatible" means (client API format; routes to Claude, OpenAI, Ollama and any OpenAI-compatible API), architecture diagram, quickstart, aliases, Claude translation, headers, keys, observability, results, deployment notes, ADR index
- Dashboard "spend per key" shows one row per key (named, revoked keys marked); the key-prefix column was dropped
- `/readyz` reads a cached dependency status refreshed every 5 s (checks run concurrently, 0.5 s timeout), so probes never wait on a hung dependency
- Docker image starts uvicorn with `--timeout-graceful-shutdown 30`
- README: use cases, FAQ (subscriptions vs API keys, Anthropic SDK / Claude Code), accuracy fixes from review

### Fixed
- Grafana refusing to start with a renderer configured and the default renderer token: the token is now shared via `GRAFANA_RENDERER_TOKEN`

## [0.6.0] - 2026-10-05 (Phase 6 — prove it: load, chaos, overhead, SLOs)

### Added
- Benchmark rig (`docker-compose.bench.yml`): mock OpenAI-compatible LLM replaying real Claude timing, a second gateway replica, Toxiproxy in front of Redis + Postgres, in-network load generator (Phase 6, ADR 0009)
- `tests/load/loadgen.py` — open/closed-loop load generator timing TTFT and every inter-chunk gap; `run_bench.py` scenarios (overhead, capacity, scaling, accuracy, breaker, chaos, lifecycle); `report.py` → `docs/RESULTS.md`
- `docs/RESULTS.md`: gateway overhead, capacity, scaling, accuracy, breaker detection/recovery, chaos, operations, SLO alerts — with charts
- SLOs and multi-window burn-rate alerts in `config/prometheus-rules.yml` (loaded by Prometheus, validated with promtool)
- `server-timing: admit;dur=…` response header (gateway admission time)
- `gateway_event_loop_lag_seconds` metric; `usage_log.estimated_tokens` (migration 0003)
- `dev_only: true` providers (like the `bench` mock) load only with `GATEWAY_ENABLE_FAKE=1`
- `gateway_ttft_e2e_seconds` (client-side time to first token, incl. fallbacks) — the TTFT SLO uses it; `gateway_auth_stale_served_total`

### Changed
- ADR 0004 documents the measured cost of a stalled provider (`attempts × first_token` before fallback)
- Time to first token is measured to the first chunk carrying output (not the empty role chunk)
- Dev stack ports bind to 127.0.0.1 only; the bench rig runs migrations once (one-shot `migrate` service) and Prometheus scrapes both replicas

## [0.5.0] - 2026-10-05 (Phase 5 — observability + cost)

### Added
- Usage log in Postgres (`usage_log`, migration 0002): one row per admitted request — key, alias, target, tokens (prompt / completion / cached), cost, latency, TTFT, attempts, fallback, status, error; written in background batches, dropped and counted rather than blocking (Phase 5, ADR 0008)
- Prometheus metrics at `/metrics` (`gateway_*`): requests, duration and TTFT histograms, tokens, cost, fallbacks, upstream attempts, rejections by reason, circuit-breaker state, dropped usage rows — labelled by alias/target/status only, never by key
- Grafana dashboard (`config/grafana/`, generated by `build_dashboard.py`) with provisioned Prometheus + Postgres datasources: spend per key, p95 latency per provider, TTFT, fallback and error rates, breaker states, tokens, cost, rejections
- Structured JSON logs (structlog) with a request id; `x-request-id` reused if valid, else generated, and echoed on every response; one usage line per request without prompt or completion content
- `cached_input` price in `pricing.yaml`; cached prompt tokens are billed at it (Anthropic cache reads)
- Metrics on a separate internal port (`metrics_port`, default 9100); Prometheus scrapes `gateway:9100`

## [0.4.0] - 2026-10-05 (Phase 4 — API keys, token-aware rate limits, budgets)

### Added
- Gateway API keys (`gw_…`): SHA-256-hashed in Postgres, tiers from `config/limits.yaml` with per-key overrides, 30 s lookup cache; `POST/GET /admin/keys`, `DELETE /admin/keys/{id}`, `make key` (Phase 4, ADR 0006)
- Every `/v1/*` call needs `Authorization: Bearer gw_…`; `/v1/models` lists only what the key may use; 403 `model_not_allowed`
- Token-aware rate limits: requests/min and tokens/min token buckets per key in Redis (one atomic Lua script), estimate before the call, reconciled with real usage after; 429 with `retry-after` and OpenAI's `x-ratelimit-*` headers on every response (ADR 0007)
- Monthly USD budget per key (Redis month-to-date spend, priced by the target that served); 429 `insufficient_quota` when exhausted
- Streams are metered: the gateway always requests upstream usage and hides the usage chunk unless the client asked for it
- Alembic migrations (`migrations/`); the dev stack and image migrate on start; `make migrate`
- `GATEWAY_STORES=memory` to run without Redis/Postgres (tests, quick local runs)
- CI runs the key-store suite against a real Postgres service
- Estimated cost is reserved against the budget at admission; disconnected requests are billed for their prompt; refusal-fallback attempts billed per model
- API-key cache: format check before lookup, separate bounded hit/miss caches, coalesced lookups, stale-if-error during Postgres outages; 2 s Postgres timeouts

### Changed
- `config/limits.yaml` gains `estimation` and a `chaos` tier; `limits.yaml` and `pricing.yaml` reload with `models.yaml`
- `app/main.py` split into `errors.py`, `streaming.py`, `metering.py`, `services.py`

### Removed
- `tiktoken` dependency (estimate + reconciliation instead, ADR 0007)

## [0.3.0] - 2026-10-05 (Phase 3 — reliability: retries, fallback, circuit breakers)

### Added
- Fallback through each alias's chain, with retries (exponential backoff, full jitter, `retry-after` honoured) — failures classified as client fault / gateway fault / transient (Phase 3, ADR 0004)
- Circuit breaker per `provider/model` target (closed → open → half-open, one probe), state in Redis via atomic Lua scripts; in-memory store for single-process use; fails open if Redis is down
- Response headers `x-gateway-attempts`, `x-gateway-fallback` (alongside `x-gateway-provider`)
- `GET /admin/providers`: circuit-breaker state per target
- Chaos provider (`type: fake`) with failure profiles (`ok`, `flaky`, `down`, `slow`, `blip`, `broken-stream`) and aliases `chaos`, `chaos-down`, `chaos-blip`
- Mid-stream failure policy (ADR 0005); `timeouts.stream_total` caps a whole stream
- Per-provider connection-pool `limits` with a short `pool` wait timeout
- CI runs the breaker suite against a real Redis service

### Changed
- Adapters replaced by a config reload are closed after a grace period instead of at shutdown
- Direct `provider/model` requests must name a known target (alias chains or per-provider model lists)
- The chaos provider and chaos aliases load only with `GATEWAY_ENABLE_FAKE=1` (set by the dev compose stack)
- `retry` and `circuit_breaker` config are validated on load/reload

## [0.2.0] - 2026-10-05 (Phase 2 — multi-provider + model registry)

### Added
- `CONTRIBUTING.md`
- Anthropic adapter (official `anthropic` SDK): OpenAI chat-completions ⇄ Messages API — system prompt, roles, `max_tokens` default, stop reasons, usage (incl. cached tokens), tool calls and parallel tool results, images, `response_format`, `reasoning_effort`, streaming events → OpenAI chunks (Phase 2, ADR 0003)
- Per-model capability flags in `config/models.yaml` (`sampling`, `forced_tool_choice`, `effort`, `refusal_fallback`) so model differences stay out of code
- Server-side refusal fallback (beta `fallbacks: "default"`) enabled for Opus 5.5 and Sonnet 5.5
- `GET /v1/models` lists the models behind the aliases as well as the aliases
- Config reload on `SIGHUP`
- Live tests (`make test-live`) for every alias against real Claude, skipped without `ANTHROPIC_API_KEY`
- `stream_idle` timeout per provider: `first_token` now bounds only the wait for the first event

### Changed
- `/admin/reload` returns 400 `invalid_config` and keeps the previous config when the YAML is broken
- `make test-e2e` never runs paid (`live`) tests
- Unconfigured provider keys produce a clear 502 ("ANTHROPIC_API_KEY is not set")

### Fixed
- Streams that end without a final event (`message_stop` / `[DONE]`) are reported as truncated instead of passing as complete
- Transport errors while reading a stream map to 504/502 or an in-band error instead of a plain-text 500

### Security
- CI token is read-only, actions pinned to commit SHAs, no persisted git credentials
- `SECURITY.md` with private vulnerability reporting, `CODEOWNERS`

## [0.1.0] - 2026-10-05 (Phase 0 foundations + Phase 1 pass-through proxy)

### Added
- `POST /v1/chat/completions`: OpenAI-compatible pass-through, non-streaming and SSE streaming (Phase 1)
- `OpenAICompatAdapter` for OpenAI and OpenAI-compatible providers (Ollama, …); `AdapterPool` reuses one HTTP client per provider
- Response header `x-gateway-provider` names the `provider/model` that served the request
- Streaming: first chunk is fetched before the 200 so upfront provider failures return real HTTP errors; mid-stream failures arrive as an in-band error event without `[DONE]` (ADR 0002)
- Client disconnect cancels the upstream request (streaming and non-streaming)
- Upstream error mapping: client-caused 400/413/422 pass through; provider auth (401/403), wrong configured model (404) and outages → 502; 429 keeps `retry-after`; `insufficient_quota` → 503; timeouts → 504; malformed upstream bodies → 502
- Request validation errors returned in OpenAI's error shape (400)
- Tests: respx-mocked endpoint tests, ASGI-level disconnect tests, `openai` SDK integration tests; `make test-e2e` against the running stack + Ollama
- Project scaffold: build plan, CLAUDE.md, SESSION.md, Claude Code agents and skills
- Docker Compose stack: gateway, Redis, Postgres, Prometheus, Grafana
- Config-driven model registry (`config/models.yaml`), pricing and limits files
- `/healthz` endpoint and config loader skeleton
- CI workflow (lint + tests)
- API tests for `/healthz`, `/v1/models`, `/admin/reload`
- `make install` / `make lock` targets; Dependabot for pip, Docker, Compose and Actions
- Config test: every model in an alias chain must have a `pricing.yaml` entry

### Changed
- Python 3.12 → **3.14** (`python:3.14.8-slim`, CI, ruff/mypy targets, `requires-python`); lockfiles recompiled, same pins
- Redis 7.4 → **8.10** (`redis:8.10.2-alpine`, via Dependabot #2)
- Postgres 16 → **18** (`postgres:18.6-alpine`); volume now mounts at `/var/lib/postgresql` (PG18 image layout). Existing dev volumes must be recreated — see SESSION.md
- CI runs on push to `main` and on PRs (no more duplicate runs per PR)
- Dependencies locked with pip-tools: `requirements*.in` (direct deps) → hashed `requirements*.txt`; Docker and CI install with `--require-hashes`
- Version floors raised to the versions actually tested
- Docker images pinned: `python:3.12.15-slim`, `redis:7.4.11-alpine`, `postgres:16.15-alpine`, `prom/prometheus:v3.15.0`, `grafana/grafana:13.2.3`
- CI: `actions/checkout` and `actions/setup-python` v7, pip cache, check that lockfiles are in sync
- Errors use OpenAI's `{"error": {...}}` shape
- CI lint step also runs mypy
- OpenAI fallbacks set to `gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra`; all prices filled in `pricing.yaml`

### Security
- Provider error text is not returned to clients except for client-caused 400/413/422 — it can contain API-key fragments, org IDs and internal hosts
- `/admin/reload` compares the admin key in constant time

<!--
Sections to use under each version:
### Added      new features
### Changed    changes to existing behaviour
### Deprecated soon-to-be removed
### Removed
### Fixed      bug fixes
### Security   vulnerabilities

## [0.1.0] - YYYY-MM-DD  (Phase 1 — pass-through proxy)
-->
