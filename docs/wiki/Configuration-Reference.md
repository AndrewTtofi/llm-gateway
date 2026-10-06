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
| `MAX_BODY_BYTES` | 33554432 (32 MiB) | Larger request bodies get a 413 |
| `CLIENT_WRITE_TIMEOUT_SECONDS` | 30 | A streaming client that doesn't take a chunk this long is disconnected; 0 = no limit |
| `DOCS_ENABLED` | `true` | `/docs`, `/redoc`, `/openapi.json`. Set `false` in production: the schema lists the admin API |
| `CACHE_REDIS_URL` | — | A separate Redis for the response cache (production: capped, LRU). Unset = `REDIS_URL` |
| `ADMIN_KEYS_FILE` | — | One operator per line: `name sha256-hex-of-key` (`make admin-key name=…`). Re-read when it changes |
| `ALERTMANAGER_SLACK_URL` | — | (production compose) Where Alertmanager posts Prometheus alerts |
| `BACKUP_KEEP_DAYS` | 14 | (production compose) How long daily `pg_dump` backups are kept |
| `ALERT_WEBHOOK_URL` | — | Slack-compatible webhook for breaker alerts ([Self-healing](Self-Healing.md)); the variable name is set by `self_healing.alert_webhook_env` |

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
| `params` | (openai type) `allow` / `drop` / `rename` / `values` / `pass`, per provider and per model. See [Providers and translation → Parameter rules](Providers-and-Translation.md#parameter-rules) |
| `default_max_tokens` | Sent as the output limit when the client sends none. Anthropic requires one (4096 unless set). For OpenAI-compatible providers it's opt-in: set it to stop unlimited answers (reasoning models can write tens of thousands of tokens), knowing that answers longer than it are cut off |
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

An alias has exactly one of three kinds of routing, plus optional extras:

```yaml
aliases:
  fixed:   { chain: [anthropic/claude-sonnet-5-5, openai/gpt-6.1-sol] }
  auto:    { policy: { optimize: cost, min_quality: 3 } }          # chosen per request
  support:                                                        # A/B test
    sticky: user
    variants:
      - { name: control, weight: 90, chain: [anthropic/claude-sonnet-5-5] }
      - { name: haiku,   weight: 10, chain: [anthropic/claude-haiku-4-5-20251001], system_prefix: "Be brief." }
    cache: { mode: exact, ttl_seconds: 3600 }                     # optional, any kind
    # semantic + scope team/global also needs shared_semantic: true (ADR 0023)
    judge: { sample_rate: 0.05, judge: smart }                    # optional, any kind
```

| Key | Page |
|-----|------|
| `policy`, `variants`, `sticky` | [Smart routing](Smart-Routing.md) |
| `cache` | [Response cache](Response-Cache.md) |
| `judge` | [Quality and safety](Quality-and-Safety.md#llm-as-judge-sampling) |

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
  failure_threshold: 5      # at least this many failures…
  failure_rate: 0.5         # …and at least this share of attempts, in the window
  window_seconds: 60
  open_seconds: 30
  probe_timeout_seconds: 330       # > the slowest call
  redis_timeout_ms: 100
self_healing:                      # background probes, quarantine, alerts (Self-Healing page)
  probes: true
  probe_interval_seconds: 10
  probe_max_tokens: 16
  quarantine_seconds: 600
  quarantine_status: [401, 403, 404]
  alert_webhook_env: ALERT_WEBHOOK_URL
  alert_min_interval_seconds: 60
```

## `config/pricing.yaml`

```yaml
checked: 2026-10-05                # bumped by `make prices ARGS=--write`
currency: USD
models:                            # $ per 1M tokens
  anthropic/claude-sonnet-5-5: { input: 2.00, output: 10.00, cached_input: 0.20, cache_write: 2.50, cache_write_1h: 4.00 }
  openai/gpt-6-astra:          { input: 10.00, output: 50.00, cached_input: 1.00, cache_write: 12.50,
                                 tiers: [{ above_prompt_tokens: 272000, input: 20.00, output: 75.00, cached_input: 2.00 }] }
  deepseek/deepseek-flash:     { input: 0.30, output: 1.20, cached_input: 0.006,
                                 off_peak: { multiplier: 0.5, peak_utc: ["01:00-04:00", "06:00-10:00"], peak_days: [mon, tue, wed, thu, fri] } }
  ollama/llama3.2:3b:          { input: 0, output: 0 }
```

| Field | Meaning |
|-------|---------|
| `cached_input` | Prompt-cache reads; missing = billed as input |
| `cache_write`, `cache_write_1h` | Prompt-cache writes (5-minute and 1-hour); missing = billed as input |
| `tiers` | Above `above_prompt_tokens`, these prices apply to the whole request |
| `off_peak` | Outside the UTC peak windows (end exclusive; `24:00` allowed), every price × `multiplier` |

Prices must be finite and non-negative; the gateway refuses to load the file otherwise.

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
  default_completion_tokens: 1024  # assumed output when the client and provider set none
  output_tokens_per_second: 100    # billing floor for requests cut off without usage (ADR 0023)
tiers:
  standard:
    requests_per_minute: 300
    tokens_per_minute: 200000
    monthly_budget_usd: 100
    allowed_aliases: [fast, balanced, smart, local]   # or provider/model names, or "*"
    injection: log                 # prompt-injection filter: off | log | flag | block
    concurrent_requests: 50        # in flight per key and replica; 0 = no limit (default 20)
teams:                             # optional; keys with `team` also count against its budget
  support: { monthly_budget_usd: 500 }
budget_alerts: [0.5, 0.8, 1.0]     # alert at these shares of a key's or team's budget (ADR 0024)
```

Per-key overrides (`POST /admin/keys`, `PATCH /admin/keys/{id}`) take precedence over the tier
for rate limits, budget and allowed aliases. `injection` and `concurrent_requests` are set
per tier only.

## `config/guardrails.yaml`

```yaml
threshold: 1.0
max_chars_per_message: 20000       # scanned per message (both ends of a longer one)
max_chars_total: 200000            # scanned per request, newest messages first
unscanned: suspicious              # text over the budget: allow | suspicious | block (ADR 0023)
rules:
  - { name: ignore_instructions, pattern: '\b(ignore|disregard)\b.{0,40}\binstructions?\b', weight: 1.0, applies_to: [user, tool] }
classifier: { alias: fast, when: suspicious, timeout_seconds: 5, max_chars: 4000 }   # optional
```

Prompt-injection rules. See [Quality and safety](Quality-and-Safety.md#prompt-injection-filter).
Patterns must compile; names are short lower-case slugs.

## Reloading

`models.yaml`, `pricing.yaml`, `catalog.yaml`, `limits.yaml` and `guardrails.yaml` reload
together without a restart, in any of three ways:

- `make reload`, which calls `POST /admin/reload`;
- `POST /admin/reload` directly;
- `kill -HUP <pid>`.

The new files are validated first. If any of them is invalid, the old config stays live and
the error is returned. Requests already in flight finish on the config they started with. Breaker state
for targets removed from every chain is dropped from metrics.
