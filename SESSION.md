# SESSION.md

Running log of where the work is. Updated at the end of every session
(by you, or by Claude via `/end-session`). Newest session on top.
Keep "Current state" short and always true.

## Current state
- **Phase:** 0 — Foundations
- **Branch:** main
- **Status:** local tooling green (`make test` 9 passed, `make lint` clean); stack not yet brought up
- **Next up:** start Docker Desktop (enable WSL integration), `.env` with API keys, install Ollama + `llama3.2:3b`, `make up`, confirm `/healthz`; `git init` + first push for CI
- **Blockers:** Docker daemon not reachable from WSL; Ollama not installed; repo not under git yet
- **Open questions:** none

---

## Session log

### 2026-10-05 — Session 2
**Did**
- Created `.venv`, installed dev deps; `make test` / `make lint` run
- Fixed ruff E501 failures in `app/main.py` (CI would have been red)
- Admin key check now uses `secrets.compare_digest` (timing-safe)
- HTTP errors returned in OpenAI error shape
- Added `tests/test_api.py` (healthz, /v1/models, /admin/reload auth)
- CI lint step now runs mypy too, matching `make lint`

**Next**
- Remaining Phase 0 items need the owner: Docker, API keys, Ollama, git remote


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
