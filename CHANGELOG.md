# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

Version plan: each completed phase bumps the minor version
(Phase 1 → 0.1.0, Phase 2 → 0.2.0 … Phase 7 → 1.0.0).

## [Unreleased]

### Added (operations, ADR 0024)
- **Budget alerts:** at 50 / 80 / 100% of a key's or team's monthly budget (`budget_alerts` in `limits.yaml`), sent through the alert webhook, plus a log line and `gateway_budget_alerts_total`. Once per level per month, fleet-wide; no extra Redis call per request.
- **Alertmanager** in the production monitoring profile: routes Prometheus alerts to `ALERTMANAGER_SLACK_URL`. Every alert links its runbook.
- **Runbooks** wiki page: one section per alert, plus backup, restore and rebuilding spend.
- **Daily `pg_dump` backups** (`backup` service, `BACKUP_KEEP_DAYS`), `restore.sh`, and `make restore-drill`.

### Changed (performance)
- **Breaker successes:** they need no Redis call unless failures are being counted (the admission check says so in the same round trip).
- **Budgets:** the early budget read is gone, since the atomic reservation is the check. A key with no budget left can still be served free cache hits.
- **Settlement:** it writes the token bucket and spend concurrently.
- **Slow streaming clients:** they're detected by one watchdog per stream instead of a timeout around every chunk.
- **Measured locally, same load:**

  | | CPU | p50 |
  |---|---|---|
  | Per request | −9% | −0.8 ms |
  | Per stream | −12% | — |

## [1.3.1] - 2026-10-06

### Fixed
- The release workflow uses only GitHub-owned actions and the `docker` CLI. The repository's Actions policy refused the third-party Docker actions, so v1.3.0's run failed at startup and **v1.3.0 has no image on GHCR**. 1.3.1 is the same code as 1.3.0, with images.

## [1.3.0] - 2026-10-06 (Phases 8–10 and the security audit)

1.2.0 was never tagged: the catalog and frontier-model work planned for it shipped here,
together with phases 9 and 10. **Upgrade notes:** run migrations (0004–0007). 0007 rewrites
`usage_log` under an exclusive lock, so on a large table run it in a quiet period. See
*Changed (ADR 0023)* for client-visible behaviour changes.

### Added (docs)
- Roadmap (`docs/wiki/Roadmap.md`): what comes after v1.3.0, and why
- The wiki is published to the GitHub Wiki tab. The `wiki` workflow re-publishes `docs/wiki/` after every merge to `main` that changes it; `scripts/publish_wiki.sh` rewrites links for the Wiki and commits as the author

### Security (audit of the gateway, ADR 0023)
A full audit found no auth bypass, injection, SSRF, secret leak or vulnerable dependency.
Its findings, all fixed with regression tests in `tests/test_hardening.py`:
- **Billing (high):**
  - Reasoning output is now counted: thinking deltas and blocks, `reasoning_content`, `reasoning`.
  - A request cut off before the provider reported usage (hang-up, timeout) is billed at least `estimation.output_tokens_per_second` (100) per second it ran, up to its `max_tokens`.
  - Attempts that timed out after reaching their provider are billed, on their own target.
- **Shared breakers (high):**
  - A full connection pool (`skipped:busy`, `503 gateway_busy`) and a request running past `total`/`stream_total` no longer count against a provider's circuit breaker. Before, one tenant could open a breaker for every tenant.
  - New per-tier `concurrent_requests` limit (`429 concurrency_limit_exceeded`; dev 10, standard 50).
  - A streaming client that stops reading for `CLIENT_WRITE_TIMEOUT_SECONDS` (30) is disconnected.
- **Token estimate:** it now counts tool definitions, file and audio parts, tool-call arguments and thinking blocks, and uses the target's real default `max_tokens`.
- **Bounds:** `max_tokens` is at most 1 000 000. When both `max_tokens` and `max_completion_tokens` are sent, `max_tokens` is dropped. Requests may have at most 10 000 messages and 1 000 parts per message. `n` must be 1–128. Over-deep JSON gets a 400 on both APIs.
- **Usage log:**
  - 64-bit token columns, and `client_request_id` (migration 0007). **Upgrade note:** 0007 rewrites `usage_log` in one statement under an exclusive lock; on a large table, run it in a quiet period.
  - A row Postgres rejects no longer takes other rows with it.
- **Provider parameters:**
  - `service_tier`, `store`, `background`, `metadata`, search options, `prediction`, `audio` and `modalities` aren't forwarded to OpenAI-compatible providers unless a provider lists them in `params.pass`.
  - `user` (and `safety_identifier`) is sent as a SHA-256 pseudonym.
- **Parsing:** chat requests check the key before parsing the body. A bad key with a 32 MB body costs 0.01 s instead of 0.25 s.
- **Guardrails:**
  - Long messages are scanned at both ends, and tool definitions are scanned.
  - Text over the scan budget is reported (`rule="unscanned"`); `unscanned: allow | suspicious | block` decides what that means. The default, `suspicious`, counts it and flags it on `flag` tiers.
- **Response cache:**
  - In team/global scope, `x-gateway-cache: refresh` acts as `bypass`.
  - Semantic matching in a shared scope needs `shared_semantic: true`.
  - Header route hints are part of the cache key.
- **Redis outages:** spend is queued and written when Redis is back, and budget checks use the last known spend instead of $0. New metric `gateway_redis_fail_open_total`, with a new alert `GatewayRedisFailingOpen`.
- **Smaller fixes:**
  - Unknown model names never become metric labels.
  - Key revokes and edits reach every replica at once through Redis pub/sub.
  - A key whose team was removed gets `403 team_unknown`.
  - `x-request-id` is always the gateway's own; a caller's id is kept as `x-client-request-id` / `usage_log.client_request_id`.
  - The read-only role's password must be ASCII (SASLprep).
  - Startup warns about an admin key under 32 characters.
- **Production deployment:**
  - `DOCS_ENABLED=false`, and Caddy blocks `/redoc`.
  - Caddy sends HSTS and has header, body and idle timeouts.
  - `migrate` and `prune-usage` get only `DATABASE_URL`.
  - The cache gets its own capped LRU Redis (`redis-cache`, `CACHE_REDIS_URL`).
  - Third-party images are pinned by digest.
  - Dependabot has a 7-day cooldown; `httpx2` is pinned directly; `.dockerignore` added.
  - New alerts: stale auth, injection spikes, concurrency rejections.

### Fixed (second review, ADR 0023)
- `n` is part of the token estimate and the budget reservation (n answers are billed)
- Budget check and reservation are one atomic step (Lua in Redis): a burst can no longer all pass the same stale check
- Streams commit to a target on the first chunk of real output, not on an empty opening chunk, so early overload errors still fall back. New timeout `first_output` (OpenAI: 300 s for silent reasoning)
- Interrupted requests without a sent output limit are capped by the model's catalog limit, not the 1 024 estimate default. OpenAI-compatible providers can send `default_max_tokens` (opt-in)
- Guardrail scans over 20 000 characters run in a worker thread instead of on the event loop
- Config reloads no longer make the adapter pool rebuild provider clients for in-flight requests (which multiplied open connections)
- The circuit breaker also needs `failure_rate` (default 50%) of attempts to fail, and a request counts once per target, so a busy healthy provider doesn't flap
- A probe cancelled during a deploy releases its slot at once
- Spend is kept in the month the request started (no refunds into the new month at midnight on the 1st)
- Upstream 429s count against the availability SLO, so a provider quota outage pages
- Judge, classifier and embedding calls are metered, with usage rows (`_judge`, `_classifier`, `_embedding`); classifier and embedding costs are charged to the key whose request caused them
- The semantic cache compares only the last user message; the history before it must match exactly
- The production Grafana role can read `judge_scores`
- `"n": null` is treated as one answer on Anthropic and Responses targets

### Changed (benchmarks)
- `docs/RESULTS.md` re-measured on the v1.3.0 request path, with new 100k-character prompt runs (`--prompt-chars`):
  - **Overhead:** +3.6 ms p50 per request, up from +2.6 ms.
  - **One replica:** within 10% of direct up to 100 concurrent streams, down from 200.
  - **Budget overshoot:** +0.9%, down from +3.4%.

### Changed (ADR 0023)
- `x-request-id` in responses is always generated by the gateway. Send your own id as `x-client-request-id` (or `x-request-id`); it comes back as `x-client-request-id`.
- Interrupted requests cost more than before. Set `estimation.output_tokens_per_second: 0` to bill only what was relayed.

### Added (Phase 10 — extensions)
- Policy routing: `model: auto` (shipped) and any alias with a `policy`. The chain is built per request from the catalog: capabilities (incl. implied), context fit, quality and price constraints, availability, then ranked by cost, quality or measured latency. Clients can tighten a policy with `route` / `x-gateway-route` (ADR 0017)
- Response cache, opt-in per alias: exact (SHA-256) or semantic (embeddings + Redis 8 vector sets). Scoped per key by default; free hits, stream replays, `x-gateway-cache: bypass | refresh` (ADR 0018)
- Self-healing: background probes of half-open targets, quarantine of auth/model/quota failures, breaker alerts to a Slack-compatible webhook, deduplicated fleet-wide (ADR 0019)
- A/B routing: weighted, sticky alias variants with optional system-prompt prefixes; `usage_log.variant` (migration 0005), per-arm metrics and Grafana panels (ADR 0020)
- Prompt-injection filter: weighted rules over normalised text in `config/guardrails.yaml`, per-tier `off | log | flag | block`, optional classifier for borderline cases (ADR 0021)
- LLM-as-judge sampling: background scoring of sampled answers against a rubric; scores and fixed labels only in `judge_scores` (migration 0006), metrics and panels (ADR 0022)

### Added (Phase 9 — close the gaps)
- Prompt caching and extended thinking through `/v1/messages`. `cache_control` and signed thinking blocks (in requests, history, responses and streams) reach Anthropic targets and come back, and never leak to other providers or OpenAI clients (ADR 0013)
- OpenAI Responses API adapter (`api: responses` per model). GPT-6 Astra and Sol can call tools again. Configurable reasoning `mode`; `store: false` always (ADR 0014)
- Long-context price tiers, cache-write prices (5-minute and 1-hour) and off-peak pricing; `make prices` proposes tiers and cache-write prices too (ADR 0015)
- `PATCH /admin/keys/{id}`; teams with monthly budgets (`limits.yaml`), `GET /admin/teams`, spend-per-team panels; `usage_log.team` (migration 0004); request body limit (`MAX_BODY_BYTES`, 413) (ADR 0016)
- Release workflow: multi-arch image to GHCR with SBOM, provenance and attestation. `docker-compose.prod.yml` (Caddy TLS, 2 hardened replicas, migration job, retention job, optional monitoring with a read-only DB role). `python -m app.maintenance prune | grant-readonly` (ADR 0016)

### Fixed (Phase 9)
- Anthropic cache writes were billed at the plain input price (1.25×–2× cheaper than the real price)
- The price sync could take another provider's LiteLLM listing for a bare model ID

### Added
- `GET /v1/catalog`: for the calling key's models, returns:
  - price and a blended price;
  - context window, capabilities and quality score;
  - breaker state and live per-model stats (latency, TTFT and error rate over this replica's last 15 minutes).
  Supports filters and sorting (ADR 0011)
- `config/catalog.yaml`: model facts plus the operator's quality score
- `make prices` (`tools/sync_prices.py`): compares pricing and catalog with LiteLLM's price list and OpenRouter's API, and prints a diff for review. `--write` applies prices both sources agree on, plus model facts. Prices that are disputed, or that only one source has, need `--force`. Remote values are validated before use
- Weekly `prices` workflow: opens, updates or closes one labelled issue as prices drift
- `pricing.yaml` has a `checked:` date; prices are validated on load (no negative or non-finite values)
- Wiki (`docs/wiki/`, published to the GitHub Wiki tab with `scripts/publish_wiki.sh`):
  - getting started, core concepts, architecture;
  - providers and translation, routing and reliability, keys/limits/budgets;
  - observability, configuration and API references;
  - choosing models, operations, testing and benchmarks, security;
  - a multi-app use case, subscriptions and provider terms, FAQ, glossary.

- Top models from each company: Claude Fable 5.1, plus new providers `gemini` (Gemini 3.1 Pro), `xai` (Grok 4.7), `mistral` (Mistral Medium 3.5) and `deepseek` (V4.1 Flash). All have verified IDs, endpoints and prices. A new `frontier` alias chains them, and `standard` keys may use it (ADR 0012)
- Per-provider and per-model parameter rules for OpenAI-compatible APIs (`params`: allow/drop/rename/values; `tools: false`, `vision: false`). A request a model can't serve skips it instead of failing the chain
- `/v1/catalog`: `configured` per model, and only the capabilities the gateway can use. Catalog facts can be `pinned`

### Changed
- `/v1/models` lists every direct model `resolve` accepts (the same set as `/v1/catalog`)
- A provider without its API key is skipped, like an open breaker: no call, no breaker effect, not counted as an attempt. If nothing else can serve, clients get a generic 503 that doesn't name the env var (previously a 502 that did)
- Live smoke tests for each new provider (`tests/e2e/test_live_providers.py`, run by `make test-live`; skipped while a key is missing)
- OpenAI requests send `max_completion_tokens`. GPT-6 Astra and Sol drop sampling parameters and skip requests with tools (tool calling needs the Responses API)

### Fixed
- OpenAI cached-input prices were missing from `pricing.yaml`, so cache reads were billed at the full input price

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
