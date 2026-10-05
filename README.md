# LLM Gateway

An OpenAI-compatible gateway for routing LLM traffic across providers, with
automatic fallback, token-aware rate limiting, per-key budgets, cost tracking
and Prometheus/Grafana observability.

Point any OpenAI SDK at it and get multi-provider reliability for free:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_dev_key")
resp = client.chat.completions.create(
    model="smart",            # an alias — resolved by config/models.yaml
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

> **Status:** 🚧 in development — see [PLAN.md](PLAN.md) for the roadmap and
> [CHANGELOG.md](CHANGELOG.md) for progress.

## Features

| Feature | Status |
|---------|--------|
| OpenAI-compatible `/v1/chat/completions` with streaming | Phase 1 |
| Providers: OpenAI, Anthropic, Ollama (local) | Phase 2 |
| Model aliases + hot-reloadable config | Phase 2 |
| Retries, fallback chains, circuit breakers | Phase 3 |
| Per-key request + token rate limits, USD budgets | Phase 4 |
| Cost tracking, Prometheus metrics, Grafana dashboard | Phase 5 |
| Load + chaos test results | Phase 6 |

## Architecture

```
client ──► auth ──► rate limiter ──► router ──► provider adapter ──► OpenAI / Anthropic / Ollama
              │          │             │                │
              │        Redis      models.yaml     circuit breaker
              └──────────── usage log ──► Postgres · /metrics ──► Prometheus ──► Grafana
```

## Quickstart

Requirements: Docker + Docker Compose, (optional) [Ollama](https://ollama.com) for a free local model.

```bash
git clone <your-repo-url> llm-gateway && cd llm-gateway
cp .env.example .env          # add your provider API keys
ollama pull llama3.2:3b       # optional local fallback model
make up
curl localhost:8000/healthz
```

| Service | URL |
|---------|-----|
| Gateway | http://localhost:8000 (docs at `/docs`) |
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |

## Changing models

All models, aliases, fallback chains and prices live in `config/`. No code changes needed:

```yaml
# config/models.yaml
aliases:
  smart:
    chain: [anthropic/claude-opus-5-5, openai/<model>, ollama/llama3.2:3b]
```

Then `make reload`. Full guide: [docs/CHANGING-MODELS.md](docs/CHANGING-MODELS.md).

## Configuration

| File | Purpose |
|------|---------|
| `.env` | Secrets and service URLs |
| `config/models.yaml` | Providers, models, aliases, fallback chains, timeouts |
| `config/pricing.yaml` | Price per 1M input/output tokens per model |
| `config/limits.yaml` | Default rate limits and budgets per API key tier |

## Development

```bash
make install     # dev deps from the hashed lockfile (requirements-dev.txt)
make test        # unit + integration (mocked providers, no cost)
make test-live   # hits real providers (costs money)
make lint        # ruff + mypy
make lock        # re-pin after editing requirements*.in (ARGS=--upgrade to bump)
```

Dependencies: edit `requirements.in` / `requirements-dev.in` (direct deps, floors),
then `make lock` regenerates the pinned, hashed `requirements*.txt`. Dependabot
opens weekly update PRs for pip, Docker images and GitHub Actions.

This repo is set up for [Claude Code](https://claude.com/claude-code): see
`CLAUDE.md`, `.claude/agents/` and `.claude/skills/`.

## Design decisions
See [docs/decisions/](docs/decisions/).

## Results
_Load and chaos test results land here in Phase 6._

## License
MIT
