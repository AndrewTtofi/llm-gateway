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
- [ ] `ProviderAdapter` interface: `chat()`, `stream()`, `health()`
- [ ] Adapters: OpenAI, Anthropic, Ollama
- [ ] Anthropic translation: system prompt, message roles, `max_tokens`, stop reasons, usage, tool calls, stream events
- [ ] `config/models.yaml` loaded at start; aliases (`fast`, `smart`, `local`) map to `provider/model`
- [ ] Hot reload of `models.yaml` (SIGHUP or `POST /admin/reload`)
- [ ] `GET /v1/models` lists aliases and models

DoD: same client code works against `fast`, `smart`, `local`; swapping a model is a YAML edit + reload, no restart.
Learn: provider API differences, adapter pattern, config-driven design.

### Phase 3 — Reliability: fallback routing (days 7–9)
Goal: provider failures are invisible to the client.
- [ ] Per-call timeouts (connect, first token, total) from config
- [ ] Retry with exponential backoff + jitter on 429/5xx/timeouts
- [ ] Fallback chain per alias; response header `x-gateway-provider` shows who served it
- [ ] Circuit breaker per provider (closed → open → half-open), state in Redis
- [ ] Mid-stream failure policy decided and documented in `docs/decisions/` (ADR)
- [ ] Fake "chaos provider" adapter for tests (random 500/429/latency)

DoD: with the primary provider forced to fail, 100% of requests succeed via fallback; breaker opens and recovers.
Learn: resilience patterns applied to LLM traffic.

### Phase 4 — Rate limiting + API keys (days 10–12)
Goal: per-client limits on requests AND tokens.
- [ ] Gateway API keys (hashed in Postgres) with per-key limits and allowed aliases
- [ ] Token bucket in Redis via atomic Lua script: requests/min and tokens/min
- [ ] Pre-request token estimate (tiktoken / char heuristic), reconcile with real `usage` after
- [ ] 429 responses with `retry-after` and `x-ratelimit-*` headers
- [ ] Monthly budget cap per key (USD), enforced

DoD: load test shows limits enforced within ±5%; budget cap blocks further calls.
Learn: why token-based limiting differs from request limiting.

### Phase 5 — Observability + cost (days 13–15)
Goal: you can see what every request cost and how every provider behaves.
- [ ] Usage log table: key, alias, provider, model, tokens in/out, latency, TTFT, cost, fallback_used, status
- [ ] `config/pricing.yaml` → cost per request
- [ ] Prometheus metrics: request count, latency histogram, TTFT, tokens, cost, fallback count, breaker state
- [ ] Grafana dashboard JSON committed (`config/grafana/`)
- [ ] Structured JSON logs with request ID; OpenTelemetry traces (optional)

DoD: dashboard shows spend per key, p95 latency per provider, fallback rate.
Learn: LLM-specific metrics (TTFT, tokens/sec, cost).

### Phase 6 — Prove it (days 16–18)
Goal: evidence it works under stress.
- [ ] k6 or Locust load test script in `tests/load/`
- [ ] Chaos scenarios: provider down, provider slow, rate-limit storm
- [ ] Gateway overhead measured (p50/p95 added latency vs direct calls)
- [ ] Results written up in `docs/RESULTS.md` with charts

DoD: numbers in the README, reproducible with one command.

### Phase 7 — Ship it (days 19–20)
- [ ] README: architecture diagram, quickstart, design decisions, results, screenshots
- [ ] Tag `v1.0.0`, update CHANGELOG
- [ ] Short write-up / LinkedIn post on what you learned
- [ ] Optional: deploy to a small VM or Cloud Run behind auth

### Phase 8+ — Extensions (later projects on this codebase)
| # | Extension | Hooks into |
|---|-----------|-----------|
| 7 | Semantic cache (embeddings + Redis vector) | before router |
| 2 | Cost autopilot (route by prompt complexity / budget) | router |
| 24 | Self-healing gateway (auto-disable, auto-recover, alerts) | breaker + metrics |
| 12 / 9 | Feature flags / prompt A/B routing | router rules |
| 21 | Prompt injection filter | request middleware |
| 25 | LLM-as-judge sampling of responses | async post-processing |

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
