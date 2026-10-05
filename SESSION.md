# SESSION.md

Running log of where the work is. Updated at the end of every session
(by you, or by Claude via `/end-session`). Newest session on top.
Keep "Current state" short and always true.

## Current state
- **Phase:** 2 — Multi-provider + model registry (built on `phase-2-providers`, in review)
- **Branch:** `phase-2-providers` · `v0.1.0` tagged on main · remote `github.com/AndrewTtofi/llm-gateway` (**public**; old private repo archived as `llm-gateway-archive`)
- **Status:** Phase 2 built + reviewed; `make test` 115 passed, `make test-e2e` 2 passed, `make lint` clean. Claude paths verified only against a mocked API
- **Next up:** owner adds `ANTHROPIC_API_KEY` to `.env` → `docker compose restart gateway` → `make test-live` (Phase 2 DoD against real Claude) → merge → tag `v0.2.0`
- **Blockers:** Phase 2 DoD for `fast`/`smart` needs the Anthropic API key
- **Open questions:** none

---

## Session log

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
