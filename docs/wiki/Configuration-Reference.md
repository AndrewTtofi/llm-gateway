# Configuration reference

Configuration has two layers:

- **Environment variables:** secrets and where services are.
- **YAML in `config/`:** models, prices and limits. This is the only place model names
  appear; the application code has none (ADR 0001).

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY` | — | Provider keys; the variable name is set per provider by `api_key_env`. A provider without its key is skipped |
| `OLLAMA_BASE_URL` | — | Referenced from `models.yaml` as `${OLLAMA_BASE_URL}` |
| `GATEWAY_ADMIN_KEY` | empty (admin API disabled) | Bearer token for `/admin/*` |
| `REDIS_URL` | `redis://localhost:6379/0` | Buckets, spend and breakers |
| `DATABASE_URL` | `postgresql+asyncpg://gateway:gateway@localhost:5432/gateway` | Keys and the usage log |
| `DB_TIMEOUT_SECONDS` | `2.0` | Pool wait, connect and query timeout on the request path |
| `METRICS_PORT` | `9100` | Internal metrics port; `0` serves `/metrics` on the API port |
| `LOG_LEVEL` | `INFO` | |
| `CONFIG_DIR` | `config` | Where the YAML lives |
| `GATEWAY_STORES` | `external` | `memory` runs without Redis/Postgres (single process, tests) |
| `GATEWAY_ENABLE_FAKE` | `false` | Loads the `fake` chaos provider and `dev_only` providers. **Never in production** |
| `GRAFANA_RENDERER_TOKEN` | dev-only value | Shared by Grafana and its image renderer (screenshots profile) |

## `config/models.yaml`

### Providers

```yaml
providers:
  anthropic:
    type: anthropic                 # anthropic | openai | fake
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY  # env var name, or null for no auth
    timeouts: { connect: 5, first_token: 30, stream_idle: 300, stream_total: 900, total: 300, pool: 5 }
    limits: { max_connections: 100, max_keepalive: 20 }
    default_max_tokens: 16000       # anthropic only: used when the client sends none
    defaults: { sampling: true, forced_tool_choice: true, effort: false, refusal_fallback: false }
    models:
      claude-sonnet-5-5: { sampling: false, forced_tool_choice: false, effort: true, refusal_fallback: true }
```

| Key | Meaning |
|-----|---------|
| `type` | Which adapter |
| `base_url` | `${VAR}` is expanded from the environment |
| `api_key_env` | The *name* of the env var holding the key, never the key itself |
| `timeouts.*` | See [Routing and reliability → Timeouts](Routing-and-Reliability.md#timeouts) |
| `limits` | Connection pool per provider; requests beyond it wait `timeouts.pool` |
| `models` | Known models with per-model capability flags (Anthropic), or failure profiles (`fake`) |
| `defaults` | Flags for models not listed |
| `stream_usage: false` | (openai type) don't send `stream_options`; usage is taken from the stream if the provider sends it, otherwise estimated |
| `params` | (openai type) `allow` / `drop` / `rename` / `values`, per provider and per model. See [Providers and translation → Parameter rules](Providers-and-Translation.md#parameter-rules) |
| `tools: false`, `vision: false` | (openai type, per model) requests needing them skip this target |
| `dev_only: true` | Loaded only with `GATEWAY_ENABLE_FAKE=1` (e.g. the benchmark mock) |

### Aliases

```yaml
aliases:
  smart:
    chain:
      - anthropic/claude-opus-5-5
      - anthropic/claude-sonnet-5-5
      - openai/gpt-6-astra
```

Order is preference. Mixing providers is what gives you resilience, since a single
provider's outage takes out every model it hosts. Ending a chain on a local model gives you
a last resort that costs nothing.

### Routing settings

```yaml
allow_direct_models: true          # clients may send provider/model (if their tier allows)
retry:
  max_attempts_per_provider: 2     # 1 = no retries
  backoff_base_ms: 250
  backoff_max_ms: 4000
  retry_on_status: [408, 409, 429, 500, 502, 503, 504, 529]
circuit_breaker:
  store: redis                     # redis (shared) | memory (per process)
  failure_threshold: 5
  window_seconds: 60
  open_seconds: 30
  probe_timeout_seconds: 330       # > the slowest call
  redis_timeout_ms: 100
```

## `config/pricing.yaml`

```yaml
currency: USD
models:
  anthropic/claude-sonnet-5-5: { input: 2.00, output: 10.00, cached_input: 0.20 }   # $ per 1M tokens
  ollama/llama3.2:3b:          { input: 0,    output: 0 }
```

Keys are targets (`provider/model`). Prices change: `make prices` compares them with public
catalogs and proposes updates for review ([Choosing models](Choosing-Models.md#keeping-prices-current)).
A target missing here logs a warning and counts as $0.

## `config/catalog.yaml`

```yaml
checked: 2026-10-05
models:
  anthropic/claude-sonnet-5-5: { context_window: 1000000, max_output_tokens: 128000, capabilities: [json_schema, reasoning, tools, vision], quality: 4 }
```

Facts for `GET /v1/catalog`. `make prices` keeps `context_window`, `max_output_tokens` and
`capabilities` in sync. `quality` (1–5) is yours. The file is optional; without it the catalog
shows prices only. See [Choosing models](Choosing-Models.md).

## `config/limits.yaml`

```yaml
estimation:
  chars_per_token: 4               # pre-call token estimate
  default_completion_tokens: 1024  # assumed output when the client sends no max_tokens
tiers:
  standard:
    requests_per_minute: 300
    tokens_per_minute: 200000
    monthly_budget_usd: 100
    allowed_aliases: [fast, balanced, smart, local]   # or provider/model names, or "*"
```

Per-key overrides (`POST /admin/keys`) take precedence over the tier.

## Reloading

`models.yaml`, `pricing.yaml`, `catalog.yaml` and `limits.yaml` reload together without a restart, in any of
three ways:

- `make reload`, which calls `POST /admin/reload`;
- `POST /admin/reload` directly;
- `kill -HUP <pid>`.

The new files are validated first. If any of them is invalid, the old config stays live and
the error is returned. Requests already in flight finish on the config they started with. Breaker state
for targets removed from every chain is dropped from metrics.
