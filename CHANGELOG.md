# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

Version plan: each completed phase bumps the minor version
(Phase 1 → 0.1.0, Phase 2 → 0.2.0 … Phase 7 → 1.0.0).

## [Unreleased]

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
