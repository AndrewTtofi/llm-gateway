# CLAUDE.md

Instructions for Claude Code working in this repo.

## Project
OpenAI-compatible LLM gateway: multi-provider routing, fallback, rate limiting,
cost tracking, observability. Full plan in `PLAN.md`. Current state in `SESSION.md`.

The owner is a platform/DevOps engineer learning AI engineering. When you write
AI-specific code (streaming, provider formats, token accounting), explain the
non-obvious parts briefly in the PR/summary so he learns from it.

## Every session
1. Read `SESSION.md` first. Work on the "Next up" item unless told otherwise.
2. Before ending: update `SESSION.md` and add entries to `CHANGELOG.md` under `[Unreleased]`.
   (Or run the `end-session` skill.)

## Stack
Python 3.12 · FastAPI · Pydantic v2 · httpx (async) · Redis · Postgres (SQLAlchemy 2 async + Alembic)
· Prometheus · Grafana · pytest + pytest-asyncio + respx · ruff + mypy · Docker Compose.

## Layout
```
app/
  main.py            FastAPI app, routes
  config.py          settings (env) + YAML loaders for config/
  schemas.py         OpenAI-format request/response models
  providers/         one adapter per provider; base.py defines the interface
  routing/           alias resolution, fallback chain, circuit breaker
  ratelimit/         token bucket (Redis + Lua)
  observability/     metrics, logging, usage records
config/
  models.yaml        aliases, models, fallback chains  ← ONLY place models are named
  pricing.yaml       $/1M tokens per model
  limits.yaml        default rate limits and budgets
tests/               unit/, integration/, load/
docs/decisions/      ADRs (NNNN-title.md)
```

## Rules
- **Never hard-code a model name, provider URL, or price in `app/`.** Read from `config/`.
  Tests may use the `fake` provider or the `local` alias.
- All provider adapters implement `app/providers/base.py::ProviderAdapter`.
- Internal format is OpenAI chat-completions. Adapters translate in and out.
- Async everywhere on the request path. No blocking calls.
- Secrets only via env vars. Never log API keys, prompts in full, or auth headers.
  Log prompt length / hash, not content.
- New behaviour needs a test. Mock providers with `respx`; never call paid APIs in unit tests.
  Integration tests against real providers are marked `@pytest.mark.live` and skipped by default.
- Errors returned to clients use OpenAI's error shape: `{"error": {"message", "type", "code"}}`.
- Non-obvious decisions → ADR in `docs/decisions/` using `0000-template.md`.

## Commands
```
make up          # start the stack
make down
make test        # unit + integration (no live calls)
make test-live   # includes live provider tests (costs money)
make lint        # ruff + mypy
make logs
make reload      # hot-reload config/models.yaml
```

## Agents
Use the agents in `.claude/agents/` for their areas: `architect` to plan a phase,
`provider-adapter`, `test-writer`, `reliability-engineer`, `observability` to build,
`code-reviewer` before merging, `docs-keeper` to update docs.

## Commits
Conventional commits: `feat(router): add circuit breaker`, `fix(stream): …`, `docs: …`.
One phase per branch: `phase-N-short-name`.
