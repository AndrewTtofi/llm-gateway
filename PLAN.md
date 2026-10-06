# LLM Gateway — Build Plan

An OpenAI-compatible gateway that sits between your apps and LLM providers, adding
multi-provider routing, fallback, rate limiting, cost tracking and observability.

**Goal:** a portfolio-grade project that teaches the production side of AI engineering
and becomes the base for later projects (semantic cache, cost autopilot, prompt
injection filter, A/B routing).

**Owner:** Andreas · **Started:** 2026-10-05 · **Target:** ~4 weeks part-time

---

## Architecture

```
 client (any OpenAI SDK)
        │  POST /v1/chat/completions   Authorization: Bearer gw_xxx
        ▼
 ┌──────────────────────── gateway (FastAPI) ────────────────────────┐
 │ auth → rate limiter → router → provider adapter → response mapper │
 │          │              │            │                            │
 │        Redis      models.yaml   anthropic / openai / ollama       │
 │                   (aliases,     (+ circuit breaker per provider)  │
 │                    fallbacks)                                     │
 │                                         │                         │
 │                    usage logger ────────┴──► Postgres             │
 │                    /metrics ───────────────► Prometheus → Grafana │
 └───────────────────────────────────────────────────────────────────┘
```

Key design rule: **nothing model-specific is hard-coded.** Every model, alias,
fallback chain and price lives in `config/`. Changing models = editing YAML.
See `docs/CHANGING-MODELS.md`.

---

## Phases

Each phase has a goal, tasks, a definition of done (DoD) and what you learn.
Don't start a phase until the previous DoD passes. Tick boxes as you go and log
every finished phase in `CHANGELOG.md`.

### Phase 0 — Foundations (day 1)
Goal: the repo runs, the stack comes up, the tooling is in place.
- [x] `docker compose up` brings up gateway, Redis, Postgres, Prometheus, Grafana
- [x] `GET /healthz` returns 200
- [ ] `.env` created from `.env.example`; API keys for Anthropic + OpenAI; Ollama installed with a small model pulled  _(done except API keys — owner adds them)_
- [x] `make test` and `make lint` run (even with zero tests)
- [x] CI workflow green on first push

DoD: fresh clone → `make up` → healthz OK in under 5 minutes.
Learn: project layout, config loading with pydantic-settings.

### Phase 1 — Pass-through proxy (days 2–3)
Goal: one provider, OpenAI format in, OpenAI format out, with streaming.
- [x] Pydantic models for the OpenAI chat request/response (`app/schemas.py`)
- [x] `POST /v1/chat/completions` forwarding to OpenAI via httpx (non-streaming)
- [x] Streaming: Server-Sent Events relayed chunk by chunk (`stream: true`)
- [x] Client disconnect cancels the upstream request
- [x] Integration test with the official `openai` SDK pointed at the gateway

DoD: `openai` SDK with `base_url=http://localhost:8000/v1` streams a reply through the gateway.
Learn: SSE, async streaming, chat completion schema.

### Phase 2 — Multi-provider + model registry (days 4–6)
Goal: aliases in YAML resolve to real models on any provider.
- [x] `ProviderAdapter` interface: `chat()`, `stream()`, `health()`
- [x] Adapters: OpenAI, Anthropic, Ollama
- [x] Anthropic translation: system prompt, message roles, `max_tokens`, stop reasons, usage, tool calls, stream events
- [x] `config/models.yaml` loaded at start; aliases (`fast`, `smart`, `local`) map to `provider/model`
- [x] Hot reload of `models.yaml` (SIGHUP or `POST /admin/reload`)
- [x] `GET /v1/models` lists aliases and models

DoD: same client code works against `fast`, `smart`, `local`; swapping a model is a YAML edit + reload, no restart.
Learn: provider API differences, adapter pattern, config-driven design.

### Phase 3 — Reliability: fallback routing (days 7–9)
Goal: provider failures are invisible to the client.
- [x] Per-call timeouts (connect, first token, total) from config
- [x] Retry with exponential backoff + jitter on 429/5xx/timeouts
- [x] Fallback chain per alias; response header `x-gateway-provider` shows who served it
- [x] Circuit breaker per provider (closed → open → half-open), state in Redis
- [x] Mid-stream failure policy decided and documented in `docs/decisions/` (ADR)
- [x] Fake "chaos provider" adapter for tests (random 500/429/latency)

DoD: with the primary provider forced to fail, 100% of requests succeed via fallback; breaker opens and recovers.
Learn: resilience patterns applied to LLM traffic.

### Phase 4 — Rate limiting + API keys (days 10–12)
Goal: per-client limits on requests AND tokens.
- [x] Gateway API keys (hashed in Postgres) with per-key limits and allowed aliases
- [x] Token bucket in Redis via atomic Lua script: requests/min and tokens/min
- [x] Pre-request token estimate (tiktoken / char heuristic), reconcile with real `usage` after
- [x] 429 responses with `retry-after` and `x-ratelimit-*` headers
- [x] Monthly budget cap per key (USD), enforced

DoD: load test shows limits enforced within ±5%; budget cap blocks further calls.
Learn: why token-based limiting differs from request limiting.

### Phase 5 — Observability + cost (days 13–15)
Goal: you can see what every request cost and how every provider behaves.
- [x] Usage log table: key, alias, provider, model, tokens in/out, latency, TTFT, cost, fallback_used, status
- [x] `config/pricing.yaml` → cost per request
- [x] Prometheus metrics: request count, latency histogram, TTFT, tokens, cost, fallback count, breaker state
- [x] Grafana dashboard JSON committed (`config/grafana/`)
- [x] Structured JSON logs with request ID; OpenTelemetry traces (optional)  _(traces deferred; request ids correlate logs + usage rows)_

DoD: dashboard shows spend per key, p95 latency per provider, fallback rate.
Learn: LLM-specific metrics (TTFT, tokens/sec, cost).

### Phase 6 — Prove it (days 16–18)
Goal: evidence it works under stress.
- [x] k6 or Locust load test script in `tests/load/`  _(own asyncio load generator: k6 can't time SSE chunks — ADR 0009)_
- [x] Chaos scenarios: provider down, provider slow, rate-limit storm
- [x] Gateway overhead measured (p50/p95 added latency vs direct calls)
- [x] Results written up in `docs/RESULTS.md` with charts

DoD: numbers in the README, reproducible with one command.

### Phase 7 — Ship it (days 19–20)
- [x] README: architecture diagram, quickstart, design decisions, results, screenshots
- [x] Tag `v1.0.0`, update CHANGELOG
- [x] Short write-up / LinkedIn post on what you learned
- [ ] Optional: deploy to a small VM or Cloud Run behind auth

### Phase 8 — Reach (done)
- [x] Inbound Anthropic Messages API (`/v1/messages`), ADR 0010
- [x] Wiki (`docs/wiki/`)
- [x] Model catalog, `GET /v1/catalog`, reviewed price sync (`make prices`), ADR 0011
- [x] Top model from each company, `frontier` alias, per-provider parameter rules, ADR 0012

### Phase 9 — Close the gaps (`phase-9-gaps`)
Things that limit real use today. Each item gets tests; non-obvious ones get an ADR.
- [x] **Lossless Anthropic features through `/v1/messages`** (ADR 0013)
  - `cache_control` on system, messages, content blocks and tools reaches Anthropic targets.
  - `thinking` (request parameter, history blocks with signatures, streamed thinking and signature deltas) round-trips.
  - Non-Anthropic targets never see these extension fields; `/v1/chat/completions` never leaks them.
- [x] **OpenAI Responses API adapter** (ADR 0014): `api: responses` per model.
  - Chat request → Responses input items, tools, `tool_choice`, `max_output_tokens`, `reasoning`, `text.format`; stream events → chunks; usage.
  - GPT-6 Astra and Sol get tool calling back. Reasoning mode `pro` is configurable per model.
- [x] **Cost accuracy** (ADR 0015)
  - Long-context price tiers (`tiers: [{above_prompt_tokens, input, output, cached_input}]`).
  - Cache-write prices (`cache_write`) and cache-creation token tracking.
  - Time-of-day pricing (DeepSeek off-peak windows).
  - The sync proposes tiers too.
- [x] **Admin** (ADR 0016)
  - `PATCH /admin/keys/{id}` (edit name, tier, limits, allowed aliases).
  - Teams: a key belongs to a team, teams have monthly budgets in `limits.yaml`, and admission checks both. Spend per team in `/admin/teams` and Grafana.
  - Request body size limit (413), configurable.
- [x] **Operations** (ADR 0016)
  - Release workflow: build and push a multi-arch image to GHCR on `v*` tags (with SBOM and provenance).
  - `docker-compose.prod.yml`: no fake provider, no reload, migration job, 2 replicas behind Caddy (TLS), secrets via env file, internal-only admin, metrics and readiness.
  - `usage_log` retention: `python -m app.maintenance prune --days N` (batched deletes) plus a documented schedule.

### Phase 10 — Extensions (`phase-10-extensions`)
- [x] **Policy routing** (ADR 0017): `model: "auto"` + `route` hints (`optimize: cost|latency|quality`, `needs`, `min_quality`, `max_blended_price`).
  - The chain is built per request from the catalog: allowed, configured, breaker not open, capabilities and context fit.
- [x] **Response cache** (ADR 0018): opt-in per alias.
  - Exact match: a hash of the normalised request, in Redis with a TTL.
  - Semantic: embeddings from a configured OpenAI-compatible provider, Redis 8 vector sets, a similarity threshold.
  - Streams are replayed; cache hits are free and metered as such.
- [x] **Self-healing and alerts** (ADR 0019)
  - Background synthetic probes for open breakers, so recovery doesn't need user traffic.
  - Longer quarantine for gateway faults (auth, quota).
  - Webhook alerts on breaker and quarantine state changes.
- [x] **A/B routing** (ADR 0020): weighted alias variants, sticky per key or user, optional system-prompt prefix per variant.
  - The variant is recorded in the usage log and metrics (bounded labels).
- [x] **Prompt-injection filter** (ADR 0021): configurable heuristics with an optional classifier model.
  - Actions per tier: `log` / `flag` / `block`.
  - Never logs the content, only the rule that matched.
- [x] **LLM-as-judge sampling** (ADR 0022): a sampled share of responses is scored asynchronously by a judge alias against a rubric.
  - Scores stored (no content) in `judge_scores`, plus a metric and a dashboard panel.

### Phase 11 — Ship it again (done)
- [x] Wiki and README for phases 9–10; wiki published to the Wiki tab and kept in sync.
- [x] Full security audit of the gateway; fix findings (ADR 0023).
- [x] Merge the stacked PRs (#10 → #14); release v1.3.0.

### What's next
See the [roadmap](docs/wiki/Roadmap.md).

---

## Claude Code agents (`.claude/agents/`)

| Agent | Model | Used in | Job |
|-------|-------|---------|-----|
| `architect` | opus | start of every phase | Plans the phase, writes ADRs, checks the design stays config-driven |
| `provider-adapter` | sonnet | 1–3, extensions | Implements/updates provider adapters and format translation |
| `test-writer` | sonnet | every phase | Writes unit + integration tests before/alongside code |
| `reliability-engineer` | sonnet | 3, 4, 6 | Retries, breakers, rate limiting, chaos + load tests |
| `observability` | sonnet | 5, 6 | Metrics, logs, Grafana dashboards, cost math |
| `code-reviewer` | opus | end of every phase | Reviews diff for bugs, security, leaks of secrets, hard-coded models |
| `docs-keeper` | haiku | end of every session | Updates CHANGELOG, SESSION.md, README sections |

Change any agent's model by editing the `model:` line in its file
(`opus`, `sonnet`, `haiku`, `inherit`, or a full model ID).

## Claude Code skills (`.claude/skills/`)

| Skill | Trigger | What it does |
|-------|---------|-------------|
| `start-session` | `/start-session` | Reads SESSION.md + PLAN.md, reports where you are and the next task |
| `end-session` | `/end-session` | Runs tests, updates SESSION.md + CHANGELOG, suggests commit message |
| `add-provider` | `/add-provider <name>` | Scaffolds adapter, config entries, tests for a new provider |
| `swap-model` | `/swap-model <alias> <provider/model>` | Edits models.yaml + pricing.yaml safely, runs smoke test |
| `phase-check` | `/phase-check` | Verifies the current phase's DoD and lists what's missing |

## Skills you need (human)

| Skill | Phase | Level needed | Resource idea |
|-------|-------|-------------|---------------|
| Python async (asyncio, httpx) | 1 | solid | FastAPI docs "async" section |
| FastAPI + Pydantic v2 | 0–1 | solid | Official tutorial |
| SSE / streaming HTTP | 1 | working | MDN Server-sent events |
| OpenAI + Anthropic API formats | 2 | detailed | Both providers' API references |
| Tokenization basics | 4 | working | tiktoken README |
| Redis + Lua scripting | 4 | working | Redis rate-limiting patterns |
| Resilience patterns (retry, breaker) | 3 | solid | You know this from DevOps |
| Prometheus/Grafana | 5 | you have it | — |
| pytest + pytest-asyncio + respx | all | working | respx docs for mocking httpx |
| Load testing (k6/Locust) | 6 | working | k6 docs |
| Docker / Compose / CI | 0 | you have it | — |

Your DevOps background covers roughly 40% of this. Focus learning time on
streaming, provider formats and token-aware limiting — those are the AI parts.

---

## Working method
1. Start every session with `/start-session`; end with `/end-session`.
2. One phase per branch: `phase-1-proxy`, `phase-2-providers`, …
3. Tests first or alongside; never merge red.
4. Every non-obvious decision gets an ADR in `docs/decisions/`.
5. Keep the real cost low: default to `haiku`/`mini`-class models and `local` (Ollama) in tests.
