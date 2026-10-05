# LLM Gateway

An OpenAI-compatible gateway for routing LLM traffic across providers, with
automatic fallback, token-aware rate limiting, per-key budgets, cost tracking
and Prometheus/Grafana observability.

Point any OpenAI SDK at it and get multi-provider reliability for free:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_...")  # make key name=me
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
| OpenAI-compatible `/v1/chat/completions` with streaming | ✅ |
| Providers: OpenAI, Anthropic, Ollama (local) | ✅ |
| Model aliases + hot-reloadable config | ✅ |
| Retries, fallback chains, circuit breakers | ✅ |
| Per-key request + token rate limits, USD budgets | ✅ |
| Cost tracking, Prometheus metrics, Grafana dashboard | ✅ |
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
make key name=me tier=dev     # prints a gw_… key once — store it
```

| Service | URL |
|---------|-----|
| Gateway | http://localhost:8000 (docs at `/docs`) |
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |

## API keys and limits

Every `/v1` call needs a gateway key: `Authorization: Bearer gw_…`. Keys get a tier from
`config/limits.yaml` — requests/min, tokens/min, a monthly USD budget and the models
they may call — and any of those can be overridden per key:

```bash
curl -s -X POST localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"batch-job","tier":"standard","tokens_per_minute":50000,"monthly_budget_usd":20}'
curl -s localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"   # + spend
```
Over a limit → `429` with `retry-after`; every response carries OpenAI's
`x-ratelimit-*` headers. Budget used up → `429 insufficient_quota`.

## Watching it fail over

```bash
# primary is down 100% of the time; every request still succeeds via the fallback
curl -si localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -H "Authorization: Bearer $GW_KEY" \
  -d '{"model":"chaos-down","messages":[{"role":"user","content":"hi"}]}' | grep x-gateway
# x-gateway-provider: fake/ok · x-gateway-fallback: true · x-gateway-attempts: 1 (breaker open)

# breaker states (admin key from .env)
curl -s localhost:8000/admin/providers -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```
`chaos-blip` has a primary that is down 20 s of every minute — keep sending requests and
watch its breaker open, go half-open and close again.

## Observability

Open **Grafana → LLM Gateway** (http://localhost:3000, admin/admin): spend per key
(from the Postgres usage log), p95 latency per provider, time to first token, fallback
and error rates, circuit-breaker states, tokens and cost per model, rejections.

- `/metrics` — Prometheus, `gateway_*` metrics (labelled by alias/target, never by key)
- `usage_log` table — one row per request, for exact per-key reports
- JSON logs with `request_id`, also returned as `x-request-id`; no prompt content, ever

The dashboard is code: edit `config/grafana/build_dashboard.py`, run it, commit both.

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

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## Design decisions
See [docs/decisions/](docs/decisions/).

## Results
_Load and chaos test results land here in Phase 6._

## License
[MIT](LICENSE)
