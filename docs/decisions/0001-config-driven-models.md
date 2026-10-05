# 0001 — All models are defined in config, not code

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 0

## Context
Model IDs, prices and best choices change every few months. The gateway must
let models be swapped without a code change or redeploy.

## Options considered
1. **Hard-code models in adapters** — simple, but every change is a deploy.
2. **Env vars per alias** — no deploy, but fallback chains get messy in env.
3. **YAML registry with aliases and ordered fallback chains, hot-reloadable** — clear, versioned in git, reviewable.

## Decision
Option 3. `config/models.yaml` is the only place models are named; `config/pricing.yaml` holds prices.

## Consequences
- Clients use stable aliases (`fast`, `smart`); the mapping evolves underneath.
- Config needs validation tests so a typo can't break routing at reload.
- `/admin/reload` must be authenticated.
