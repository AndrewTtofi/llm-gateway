# SESSION.md

Running log of where the work is. Updated at the end of every session
(by you, or by Claude via `/end-session`). Newest session on top.
Keep "Current state" short and always true.

## Current state
- **Phase:** 8 — extensions. First: inbound Anthropic Messages API (`phase-8-messages-api`)
- **Branch:** `phase-8-cost-catalog` (on top of `docs-wiki`) · `v1.0.0` tagged + released on main · remote `github.com/AndrewTtofi/llm-gateway` (public)
- **Status:**
  - PR #9 (`/v1/messages`, v1.1.0) is waiting on CI after the GitHub outage.
  - The `docs-wiki` branch has the wiki (17 pages + publish script).
  - `phase-8-cost-catalog` has the model catalog, `/v1/catalog` and `make prices` + the weekly drift issue (ADR 0011). `make test` 322.
  - `phase-8-frontier-models` (on the catalog branch) adds Fable 5.1, Gemini, xAI, Mistral, DeepSeek, the `frontier` alias and per-provider parameter rules (ADR 0012). `make test` 356. The new providers are untested live: no keys yet.
- **Next up:**
  1. Merge #9 → tag v1.1.0, then the wiki PR, then the catalog PR.
  2. Owner: enable Wikis and restrict editing to collaborators, then run `scripts/publish_wiki.sh`.
  3. Then policy routing (`model: auto` + hints, built on the catalog), or lossless caching/thinking for `/v1/messages`.
- **Blockers:** none. Owner: confirm the Anthropic API key was rotated; `OPENAI_API_KEY` still empty (OpenAI fallbacks untested live)
- **Open questions:** deploy target (Cloud Run / ECS / VM), still optional

---

## Session log

### 2026-10-06 — Session 13
**Did**
- Phase 9 (`phase-9-gaps`), ADRs 0013–0016:
  - prompt caching and thinking through `/v1/messages`;
  - OpenAI Responses API adapter (Astra and Sol call tools again);
  - long-context tiers, cache-write prices and off-peak pricing;
  - `PATCH /admin/keys`, teams with budgets, body limit;
  - release workflow (GHCR, SBOM, provenance), `docker-compose.prod.yml`, retention job, read-only Grafana role.
- The production stack was brought up locally from a local image, twice (before and after review):
  - TLS, 2 replicas, per-replica balancing;
  - operator listener, read-only role, retention;
  - secrets scoped per container.
- Code review fixes:
  - **Caddy:** dynamic upstreams, plus an operator listener in place of `remote_ip` matching.
  - **Leaks:** usage extension fields leaking to OpenAI clients; OpenAI clients could turn thinking on.
  - **Requests:** system prompts arriving as part lists at non-Anthropic providers; truncated chunked bodies on disconnect.
  - **Database role:** the password is sent as a SCRAM verifier, through driver-level DDL.
  - **Validation:** off-peak windows checked and billed at request start; batch size 0 rejected.

**Learned**
- SQLAlchemy `text()` treats `:word` inside string literals as bind parameters; run DDL with `exec_driver_sql`
- Source-IP allowlists are unreliable behind proxies and Docker's userland proxy: put operator endpoints on a separate listener
- redis-py's VSIM raises AttributeError when the vector set doesn't exist yet

### 2026-10-05 — Session 12
**Did**
- Researched subscription terms across providers (wiki: Subscriptions-and-Terms). No credential reuse. Pass-through is documented for Claude Code and Codex but not built
- Wiki in `docs/wiki/` (17 pages incl. a multi-app use case), `scripts/publish_wiki.sh`
- Model catalog (`config/catalog.yaml`), `GET /v1/catalog` (prices, capabilities, quality, breaker, live stats), `make prices` sync with review, weekly drift workflow. ADR 0011

**Learned**
- No provider exposes prices via API; LiteLLM (provider IDs, capabilities) + OpenRouter (cross-check) agree on current prices
- The sync immediately found missing OpenAI cached-input prices (cache reads were billed at full input price)

### 2026-10-05 — Session 11
**Did**
- v1.0.0 merged, tagged, GitHub release. Commits from now on authored only by the owner (no co-author trailer)
- `/v1/messages` (Anthropic Messages API inbound, translated at the edge; ADR 0010), `count_tokens`, `x-api-key` auth,
  pluggable stream wire format. 21 tests incl. the real Anthropic SDK (streaming tool calls, typed errors, fallback Claude → OpenAI-compatible)
- Ran real Claude Code against the dev stack (`fake/ok`): found it sends `role: "system"` inside `messages` → now supported

**Learned**
- Anthropic streams numbered content blocks with open/delta/stop events; OpenAI streams flat deltas → open a new block when the output kind changes
- Usage comes last in OpenAI streams, so Anthropic's message_delta must wait for the end
- Dropping `cache_control` in translation means no prompt caching: matters a lot for agent workloads

### 2026-10-05 — Session 10
**Did**
- Phase 7: README rewrite (leads with "OpenAI-compatible = the client API; Claude is the
  default primary"), mermaid architecture, deploy notes; `/readyz`; Grafana image renderer
  (profile `screenshots`) → `docs/img/dashboard.png`; `docs/WRITEUP.md`; v1.0.0

**Learned**
- Grafana 13 refuses to start with a renderer URL and the default renderer token: set a shared one
- Readiness must not fail on shared deps (Redis/Postgres) or one blip removes every replica
- Demo traffic must use realistic ratios: priced calls at 100% just show budgets exhausted

### 2026-10-05 — Session 9
**Did**
- Phase 6: bench rig (mock LLM replaying measured Claude timing, 2nd replica, Toxiproxy),
  own load generator (per-chunk timing, open/closed loop), 9 scenarios, SLO burn-rate alerts,
  docs/RESULTS.md + README numbers. ADR 0009

**Results (laptop, after the review's methodology fixes)**
- Overhead +2.6 ms p50 / +3.8 ms p95 (paired), +4.2 ms to realistic TTFT (0.8%)
- One replica within 10% of direct up to 200 streams, core saturates at 400; 1→2 replicas 445→614 req/s
- Rate limits -0.06% across 2 replicas; budget overshoot +3.4%; breaker: 5 requests paid, 0 client errors
- 100% served through provider/Redis/Postgres failures; 90/90 streams survived SIGTERM; fast-burn alert fired

- code-reviewer found the *method* flawed in places: budget overshoot and token-estimate
  numbers were mock artifacts (mock ignored max_tokens), the +8 ms TTFT was within noise,
  capacity clients ran in lockstep, breaker recovery was set by the scenario's timing,
  stale-if-error was never exercised, the TTFT SLO measured the serving attempt only,
  bench keys could spend real money. All fixed; whole suite re-run; report derives every
  claim from the data

**Learned**
- Pair comparisons (same seed → same mock delays) to remove the mock's own noise
- Prometheus keeps data in an anonymous volume: `--force-recreate` needs `-V` for a clean slate
- Measure the rig before the system: direct-to-mock baselines showed the load generator, not
  the gateway, saturating first; alternating runs exposed a cold-start "2.7× scaling"
- Report latency over *all* responses, and align fault injection by wall clock
- A stalled provider costs attempts × first_token before fallback (retries on timeouts)
- Silent no-op string edits: always assert a replacement matched


### 2026-10-05 — Session 8
**Did**
- Phase 5: usage_log (Postgres, background batch writer), Prometheus metrics (bounded labels),
  Grafana dashboard as code + provisioned datasources, JSON logs with request ids,
  cached-token pricing. ADR 0008. Demo keys `team-a`, `team-b` left in the dev DB so the
  dashboard has data

- code-reviewer pass: 2 blockers (unknown models recorded as 200 with the client's model
  string as a metric label → unbounded series; one oversized value sank a 100-row batch)
  + 11 should-fix (shutdown lost the in-progress batch, failures flagged as fallbacks,
  mid-stream disconnects as clean 200s, fallback latency blamed on the healthy provider,
  /metrics on the public port, …). All fixed with tests; usage_log.id → BIGINT
- Re-verified: all 17 panels return data after 400 mixed requests

**Decided**
- Spend per key from Postgres (exact, unbounded keys), not Prometheus labels (cardinality)
- Usage writes never block requests: queue + batch, drop and count when Postgres is slow/down
- Pure ASGI middleware for request ids (BaseHTTPMiddleware would break disconnect detection)

**Learned**
- Grafana's /api/ds/query runs a panel's real queries: a good DoD check that a dashboard
  shows data, not just that it loads


### 2026-10-05 — Session 7
**Did**
- Phase 4: API keys (hashed, Postgres via Alembic, cached lookup, admin API), token-aware rate
  limits (two Redis token buckets per key, one Lua script, estimate → reconcile), 429s with
  OpenAI-style headers, monthly USD budgets, metered streams. Split main.py into modules.
  ADR 0006 (keys), 0007 (limits/budgets)

**Decided**
- SHA-256 for keys (random, not passwords); 30 s cache → revocation within 30 s
- No tiktoken: char estimate + reconciliation with real usage
- Budget/limits fail open when Redis is down; auth fails closed when Postgres is down (cached keys keep working)

- code-reviewer pass: 2 control bypasses (token debt forgiven by a 120 s Redis TTL → ~10× the
  limit with huge prompts; hanging up before the answer cost $0) + 10 should-fix (budget
  overshoot by everything in flight, key-flood cache eviction, no Postgres timeouts, refusal
  iterations unbilled, …). All fixed with tests; the three key fixes mutation-checked

**Learned**
- `x += await f()` loses updates across coroutines — Python reads `x` before suspending
- A load test must saturate the limiter, or it measures the client


### 2026-10-05 — Session 6
**Did**
- Phase 3: router with retries/backoff/jitter + fallback chains, circuit breaker per target in
  Redis (Lua) with memory store, chaos provider + aliases, stream_total deadline, pool limits,
  retiring old adapters after a grace period. ADR 0004 (retries/fallback/breaker), 0005 (mid-stream)

**Decided**
- Breakers per `provider/model`, not per provider (Anthropic overload is per model)
- Fail classification: client fault returns, gateway fault falls back without retry, transient retries then falls back
- No mid-stream fallback: error in-band, breaker learns (ADR 0005)

- code-reviewer pass: 1 blocker (committed-stream wrapper never closed the upstream if the client
  left during the first chunk — a Phase 1 regression), probe slot leaks, stragglers closing open
  breakers, Redis latency on every request, error ranking, unbounded breaker state via direct
  model names, fake provider reachable in prod, CROSSSLOT keys, grace period too short. All fixed
  with tests; blocker + probe release mutation-checked
- Live demo on chaos-blip: open → half-open probe → closed, 137/137 requests succeeded

**Learned**
- Unit tests silently used the real local Redis through the app lifespan — test config must pin `store: memory`
- `aclose()` on an async generator that never started doesn't run its `finally`; a wrapper that
  must clean up has to be a class, not a generator
- A breaker must only let the probe close it, or in-flight stragglers make it flap


### 2026-10-05 — Session 5
**Did**
- Phase 2 on `phase-2-providers`: Anthropic adapter via official SDK, translation module with unit tests,
  capability flags per model in config, refusal fallback (beta), `/v1/models` + SIGHUP + safe reload
- code-reviewer pass found 3 blockers + 9 should-fix, all fixed with tests (mutation-checked):
  unmapped stream transport errors, assistant-first/system-only requests, malformed input → 500,
  temperature+top_p, `""` args for no-arg tools, declined model's tool calls leaking past a
  refusal fallback, usage iterations, schema transform, user hashing, truncated streams,
  first_token vs stream_idle, image media types
- 115 tests; live tests ready but skipped (no key)

**Decided**
- Official `anthropic` SDK over raw httpx (ADR 0003); mocks via `httpx2.MockTransport`
- Repo goes public. History rewritten so commits use the GitHub noreply email; recreated as a fresh
  public repo (PR numbers restarted — `#N` references in older notes point at the archive)
- Never commit with the personal email; provider keys only in `.env`, never in chat or the repo

**Learned**
- Model differences (sampling params, forced tool choice, effort) belong in config, not `if model ==`
- SDK raises a mid-stream `error` event as `APIStatusError` with status 200


### 2026-10-05 — Session 4
**Did**
- Phase 1 on `phase-1-proxy`: `/v1/chat/completions` pass-through, SSE streaming, disconnect cancellation, OpenAI error mapping
- 47 unit/integration tests + 2 e2e; DoD met: `openai` SDK streams from Ollama through the running gateway
- ADR 0002: streaming error semantics, disconnects, what provider error text clients may see
- code-reviewer pass: fixed disconnect during first-token wait, upstream left open on slow-client
  disconnect (`SSEResponse`), provider error text leaking key fragments/org IDs, raw 500s on
  malformed upstream bodies, swallowed outside cancellation, 404/insufficient_quota mapping

**Learned**
- In SSE the HTTP status is final once headers go out — so pull the first chunk before committing to 200
- Streamed responses carry deltas, not the full message; usage only arrives (as a final chunk with empty `choices`) when `stream_options.include_usage` is set

**Deferred to Phase 3** (from review)
- Per-provider connection-pool limits + short pool timeout; overall stream deadline;
  close reload-retired adapters after a grace period; read retry statuses from config

**Next**
- Phase 2: Anthropic adapter (translation notes in the Claude discussion: thinking always-on, no forced tool_choice, refusal stop reason)


### 2026-10-05 — Session 3
**Did**
- Dependency audit: installed versions were all latest, but nothing was pinned (only `>=` floors)
- Added pip-tools locks with hashes, raised floors, pinned Docker images, Dependabot (branch `chore/pin-deps`)

**Decided**
- Kept Redis 7 / Postgres 16 / Python 3.12 majors; Postgres 18 needs a pgdata migration — separate decision
- Standard pip-tools layout (`.in` → `.txt`) so Dependabot can regenerate locks

- Merged PR #1. Then major upgrades on `chore/major-upgrades`: Python 3.14.8, Redis 8.10.2 (8.8.3 in #5, then Dependabot #2), Postgres 18.6
- Postgres 18 image stores data under `/var/lib/postgresql/18/docker`; mount moved to `/var/lib/postgresql`.
  Old PG16 volume had 0 tables — dumped (`pg_dumpall`) then removed and recreated
- Local `.venv` rebuilt on Python 3.14.8 (via `uv python install`, no system changes)

**Learned**
- Upgrading Postgres majors in Docker is never just a tag bump: data dirs are version-specific (pg_upgrade or dump/restore)

**Next**
- Merge `chore/major-upgrades`, then Phase 1


### 2026-10-05 — Session 2
**Did**
- Created `.venv`, installed dev deps; `make test` / `make lint` run
- Fixed ruff E501 failures in `app/main.py` (CI would have been red)
- Admin key check now uses `secrets.compare_digest` (timing-safe)
- HTTP errors returned in OpenAI error shape
- Added `tests/test_api.py` (healthz, /v1/models, /admin/reload auth)
- CI lint step now runs mypy too, matching `make lint`

- Created `.env` from example with a generated `GATEWAY_ADMIN_KEY` (API keys still empty)
- `make up`: all 5 services healthy; `/admin/reload` works with the admin key
- Pulled `ollama/llama3.2:3b`; verified the container reaches Ollama via `host.docker.internal`
- Filled OpenAI models: fast → `gpt-6-luna`, balanced → `gpt-6.1-sol`, smart → `gpt-6-astra`
- Filled all prices in `pricing.yaml` (checked 2026-10-05); test that every chain model is priced
- `git init`, repo-local identity, pushed to private GitHub repo; first CI run green

**Decided**
- Repo is private for now; flip to public at Phase 7

**Next**
- Owner: add provider API keys to `.env`
- Phase 1 — pass-through proxy


### 2026-10-05 — Session 1
**Did**
- Chose project: LLM gateway (#11) as first AI engineering project
- Generated repo scaffold: PLAN, CLAUDE.md, agents, skills, Docker stack, config files

**Decided**
- Python + FastAPI (AI ecosystem language) over Go
- OpenAI-compatible API surface
- All models/prices in `config/` so models can be swapped without code changes

**Next**
- Phase 0 tasks in PLAN.md

---

<!-- Template for new entries (copy above the previous session)

### YYYY-MM-DD — Session N
**Did**
-
**Decided**
-
**Learned**
-
**Next**
-
**Blockers**
-
-->
