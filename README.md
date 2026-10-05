# LLM Gateway

A self-hosted gateway that sits between your applications and LLM providers. Apps talk to
**one endpoint** in the OpenAI request format, which most SDKs and tools already speak. The gateway picks the provider, falls back when one
fails, enforces per-key rate limits and budgets, and records what every request cost.

**"OpenAI-compatible" describes the API the gateway exposes, not the providers behind it.**
Clients use the OpenAI chat-completions format (`/v1/chat/completions`) or the **Anthropic
Messages API** (`/v1/messages`, for the Anthropic SDKs and Claude Code). Internally every
request is converted to one format, which is what lets a request that started on Claude fall
back to another provider. Behind the gateway, requests can go to:

| Provider | How |
|----------|-----|
| **Anthropic Claude** (Opus, Sonnet, Haiku) | Native adapter on the official SDK. It translates messages, tools, streaming, usage and errors both ways. Claude is the first choice in most built-in aliases. |
| **OpenAI** | Passthrough (covered by mocked tests; not yet run against the live API) |
| **Ollama** (local models, free) | Ollama's OpenAI-compatible API |
| **Any OpenAI-compatible API** (vLLM, LM Studio, OpenRouter, Together, …) | Add it in `config/models.yaml`; no code change |

```python
from openai import OpenAI   # any OpenAI SDK (or LangChain, LlamaIndex, curl, …)

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_...")   # a gateway key
resp = client.chat.completions.create(
    model="smart",   # an alias: Claude Opus → Claude Sonnet → OpenAI, in that order
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

The client never learns which provider answered unless it reads the `x-gateway-provider` header.

![Grafana dashboard: request rate, fallback rate, p95 latency, time to first token, circuit breakers, spend per key](docs/img/dashboard.png)

<sub>Local demo traffic. The spend figures come from a fake provider priced high on purpose to exercise budgets; they are not real spend.</sub>

## Why

When several apps call LLMs directly, each app repeats the same work and the same failures:

- one provider SDK per app;
- an outage at one provider is an outage for your users;
- no one can say which team spent $4,000 last month;
- a runaway batch job can burn the whole budget.

The gateway moves all of that into one place that the platform team owns:

| | |
|---|---|
| **Routing** | Apps ask for an alias (`fast`, `smart`, `balanced`, `local`). `config/models.yaml` maps each alias to an ordered chain of models. To change models, edit YAML and run `make reload`; apps don't change. |
| **Reliability** | Transient errors are retried with jittered backoff, then the next model in the chain is tried. A circuit breaker per model, shared across replicas in Redis, stops traffic to a provider that is down. |
| **Streaming** | SSE relay. Errors before the first token can still fall back. After the first token, errors are reported in-band so a client never gets half of one answer spliced to another. When a client disconnects, the upstream request is cancelled. |
| **Limits** | Every key has requests/min, tokens/min (estimated up front, corrected to actual usage afterwards) and a monthly USD budget. Admission is one atomic Redis Lua script, so limits hold across replicas. |
| **Cost** | Each request is priced from `config/pricing.yaml`, including cached-token rates, and written to a Postgres usage log for per-key reports. |
| **Observability** | Prometheus metrics (never labelled by key), a Grafana dashboard provisioned as code, SLO burn-rate alerts, and JSON logs with a request id. Prompt content is never logged. |

## Documentation

The **[wiki](docs/wiki/Home.md)** explains every part in depth:

- architecture and the life of a request;
- provider translation;
- routing and circuit breakers;
- keys, limits and budgets;
- observability, configuration, the API, operations and security;
- [a multi-app walkthrough](docs/wiki/Use-Case-Multi-App.md);
- [subscriptions and provider terms](docs/wiki/Subscriptions-and-Terms.md).

## Use cases

| Situation | What the gateway does |
|-----------|-----------------------|
| **A product feature depends on one LLM provider.** When the provider has an outage or returns 429/529 "overloaded", the feature goes down. | The `smart` / `fast` chains fail over to a second provider, or a local model, before the first token. Users see a slightly different model, not an error. |
| **Several teams share an Anthropic/OpenAI account.** Nobody knows who spent what, and one runaway job can drain the month's budget. | One key per team or service, with its own tokens/min and monthly USD budget. The dashboard and `usage_log` show exact spend per key. A key that hits its budget gets `429 insufficient_quota` instead of a surprise invoice. |
| **A support chatbot or other customer-facing assistant.** Latency and availability matter more than which model answers. | Streaming with time-to-first-token SLOs and burn-rate alerts. Circuit breakers stop sending traffic to a degraded provider within seconds. |
| **Overnight batch jobs** (summarising tickets, tagging documents, evaluations) compete with interactive traffic for the same provider rate limits. | Give the batch key a low tokens/min and its own budget so it can't starve the chatbot. Route it to a cheaper alias such as `fast`. |
| **Moving to a new model**, or comparing a cheaper one. | Change the alias chain in `config/models.yaml` and run `make reload`. Every app moves at once, with no deploys. Per-target latency, error and cost panels show whether the new model is better. |
| **Sensitive or offline workloads, or a dev laptop with no API budget.** | The `local` alias routes to Ollama on your own hardware, so prompts never leave the machine. Ollama also serves as the last fallback in the `fast` and `balanced` chains. |
| **Internal tools built on LangChain, LlamaIndex, the OpenAI SDK or the Anthropic SDK.** | Point `base_url` at the gateway and use a gateway key. Provider API keys stay in one place and never reach app config or laptops. |
| **Developers using Claude Code** on company API keys. |  Point `ANTHROPIC_BASE_URL` at the gateway. Each developer or team gets a key with its own budget, and usage shows up on the same dashboard as everything else. |
| **Security and compliance want an audit trail.** | Every request has a usage row (key, model, tokens, cost, latency, status) and a request id that correlates with logs. Prompt content is never stored. |

## Architecture

```mermaid
flowchart LR
    C["Apps<br/>(OpenAI SDK, curl, LangChain…)"] -->|"POST /v1/chat/completions<br/>Bearer gw_…"| A

    subgraph GW["Gateway (FastAPI, async; N replicas)"]
        A["Auth<br/>hashed keys, cached"] --> L["Rate limit + budget<br/>token buckets"]
        L --> R["Router<br/>alias → fallback chain<br/>retries · circuit breaker"]
        R --> P["Provider adapters<br/>translate in/out"]
    end

    P --> AN["Anthropic"]
    P --> OA["OpenAI"]
    P --> OL["Ollama / any<br/>OpenAI-compatible"]

    A -.-> PG[("Postgres<br/>keys · usage log")]
    L -.-> RD[("Redis<br/>buckets · breakers")]
    R -.-> RD
    GW -.->|"usage rows (batched)"| PG
    GW -.->|":9100/metrics"| PR["Prometheus<br/>+ SLO alerts"] --> GF["Grafana"]
    CFG["config/*.yaml<br/>models · pricing · limits"] -.->|"hot reload"| R
```

Request path: authenticate → reserve rate-limit tokens and budget → resolve the alias →
try each target in the chain (skipping any whose breaker is open) → stream the answer →
reconcile actual tokens and cost → write a usage row in the background.

If Redis or Postgres fails, the gateway degrades instead of going down:

- **Redis down:** rate limits, budgets and breakers fail open.
- **Postgres down:** recently used keys are served from cache, keys not in the cache get `503`, and usage rows are dropped and counted.

Chat traffic keeps flowing in both cases. [RESULTS.md](docs/RESULTS.md) has the chaos tests.

## Quickstart

You need Docker with Compose. [Ollama](https://ollama.com) is optional and gives you a free local model.

```bash
git clone https://github.com/AndrewTtofi/llm-gateway.git && cd llm-gateway
cp .env.example .env            # add ANTHROPIC_API_KEY / OPENAI_API_KEY and an admin key
ollama pull llama3.2:3b         # optional: local model, also the last fallback
make up                         # gateway, Redis, Postgres, Prometheus, Grafana
make key name=me tier=dev       # prints a gw_… key once; store it
export GW_KEY=gw_...
```

```bash
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"Say hi in five words"}]}'
```

| Service | URL |
|---------|-----|
| Gateway (API docs at `/docs`) | http://localhost:8000 |
| Grafana (admin / admin) | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

The dev stack binds every port to `127.0.0.1`.

### Watch it fail over (no API keys needed)

The dev stack loads a `fake` provider with failure profiles. Create a key on the `chaos` tier:

```bash
make key name=demo tier=chaos   # → export GW_KEY=gw_…

# chaos-down: the primary always fails. Every request still succeeds.
curl -si localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"chaos-down","messages":[{"role":"user","content":"hi"}]}' | grep x-gateway
# x-gateway-provider: fake/ok
# x-gateway-fallback: true
# x-gateway-attempts: 1     ← after the first few requests: the breaker has opened,
#                              so the dead primary isn't even tried
```

The primary of `chaos-blip` is down for 20 s of every minute. Keep sending requests and
watch its breaker open, go half-open and close again on the Grafana dashboard. You can
also check breaker state directly:

```bash
curl localhost:8000/admin/providers -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```

## Using it

### Aliases

Clients send an alias as `model`. Each alias maps to a chain of models (from `config/models.yaml`):

| Alias | Chain |
|-------|-------|
| `fast` | Claude Haiku 4.5 → OpenAI → Ollama llama3.2 |
| `smart` | Claude Opus 5.5 → Claude Sonnet 5.5 → OpenAI |
| `balanced` | Claude Sonnet 5.5 → OpenAI → Ollama llama3.2 |
| `local` | Ollama llama3.2 |

Clients can also call an exact `provider/model` if their tier allows it. `GET /v1/models`
lists what the key may use.

### Anthropic clients (SDK, Claude Code)

`POST /v1/messages` accepts the Anthropic Messages API. Responses, streaming events and
errors come back in Anthropic's format. Authenticate with `x-api-key` or
`Authorization: Bearer`, using a gateway key.

```python
import anthropic

client = anthropic.Anthropic(base_url="http://localhost:8000", api_key="gw_...")
msg = client.messages.create(
    model="smart", max_tokens=512, messages=[{"role": "user", "content": "Hello"}]
)
```

To route Claude Code through the gateway:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_AUTH_TOKEN=gw_...                 # a gateway key (see the note on tokens/min below)
export ANTHROPIC_MODEL=smart                        # gateway aliases, not Anthropic model IDs
export ANTHROPIC_DEFAULT_HAIKU_MODEL=fast
claude
```

The request is translated into the internal format, so the alias's whole fallback chain
applies, including non-Claude models. Some features don't survive the translation yet:

- **Prompt caching:** `cache_control` is dropped.
- **Extended thinking:** dropped.
- **Rejected with a 400:** server tools (web search, code execution) and document blocks.

Claude Code asks for a large `max_tokens` (often 32 000) on every request. The rate limiter
reserves the prompt plus `max_tokens` up front and returns the unused part when the request
finishes, so a Claude Code key needs a tier with a tokens/min well above that, or a per-key
`tokens_per_minute` override.

`/v1/messages/count_tokens` returns the gateway's estimate, not an exact count. Each call counts
as one request against the key's limit.
[ADR 0010](docs/decisions/0010-anthropic-messages-inbound.md) has the details.

### What gets translated for Claude

The Anthropic adapter converts the following, so OpenAI-format clients can use Claude unchanged:

- **Messages:** system messages, multi-turn history, images.
- **Tools:** tool calls and tool results.
- **Parameters:** `tool_choice`, `reasoning_effort` → `effort`, and `stop`.
- **Usage:** reported in OpenAI's shape, including cached prompt tokens.
- **Responses:** finish reasons, plus error types mapped to OpenAI's error shape.

Model capabilities are flags in config rather than code. For example, newer Claude models
reject `temperature`, so the adapter drops it for them. [ADR 0003](docs/decisions/0003-anthropic-adapter.md) has the details.

### Response headers

| Header | Meaning |
|--------|---------|
| `x-gateway-provider` | Which `provider/model` served the request |
| `x-gateway-fallback` | `true` if it wasn't the chain's first choice |
| `x-gateway-attempts` | Upstream calls made, including retries |
| `x-ratelimit-*`, `retry-after` | OpenAI-style rate-limit headers |
| `x-request-id` | Correlates with the gateway's logs and usage row |
| `server-timing: admit;dur=…` | Time the gateway spent on auth and limits |

Errors use OpenAI's shape (`{"error": {"message", "type", "code"}}`):

- **Over a rate limit:** `429 rate_limit_exceeded` with `retry-after`.
- **Budget used up:** `429 insufficient_quota`.

### API keys and limits

Keys look like `gw_…`. They are shown once and stored only as a SHA-256 hash. Each key has
a tier from `config/limits.yaml`:

| Tier | Requests/min | Tokens/min | Budget/month | Aliases |
|------|-------------|-----------|--------------|---------|
| `dev` | 60 | 50 000 | $10 | `fast`, `local` |
| `standard` | 300 | 200 000 | $100 | `fast`, `balanced`, `smart`, `local` |

Any field can be overridden per key:

```bash
curl -X POST localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"batch-job","tier":"standard","tokens_per_minute":50000,"monthly_budget_usd":20}'
curl localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"     # keys + spend
curl -X DELETE localhost:8000/admin/keys/<id> -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```

### Changing models

All models, aliases, chains and prices live in `config/`. The application code contains no model names:

```yaml
# config/models.yaml
aliases:
  support-bot:   # a new alias: apps send model="support-bot"
    chain: [anthropic/claude-sonnet-5-5, openai/<model>, ollama/llama3.2:3b]
```

Run `make reload` and the change applies without a restart. [docs/CHANGING-MODELS.md](docs/CHANGING-MODELS.md)
covers adding a provider, a model or a price.

| File | Purpose |
|------|---------|
| `.env` | Secrets and service URLs (see `.env.example`) |
| `config/models.yaml` | Providers, models, capability flags, aliases, timeouts, retry and breaker settings |
| `config/pricing.yaml` | $ per 1M input / output / cached tokens per model |
| `config/limits.yaml` | Rate limits, budgets and allowed aliases per tier |

## Observability

Grafana provisions the **LLM Gateway** dashboard at startup (pictured above). It shows:

- request and error rates, fallback rate by alias, and p95 latency per provider;
- time to first token, both end to end and per target;
- a circuit-breaker timeline;
- upstream attempts by outcome;
- tokens and spend per target;
- rejections by reason;
- spend per key.

The dashboard is code: edit `config/grafana/build_dashboard.py`, run it, and commit both.

- **Metrics:** `gateway_*` on an internal port (`:9100/metrics`). Labels come from bounded sets (alias, target, status, outcome, reason, …), never the API key.
- **Usage log:** the `usage_log` table has one row per request: key, alias, target, tokens, cost, latency, TTFT, attempts and status.
- **Logs:** JSON lines with `request_id`. They record prompt length, never content.
- **SLOs:** `config/prometheus-rules.yml` has multi-window burn-rate alerts on the availability error budget and a p95 TTFT alert. It also alerts on an open breaker, dropped usage rows, event-loop lag and the gateway being down.

## Results

These numbers come from one laptop and a mock provider. The mock replays measured Claude
Haiku timing. Runs are paired request by request, so the mock's own randomness cancels out.
[docs/RESULTS.md](docs/RESULTS.md) has the method, charts and raw numbers.

| | |
|---|---|
| Gateway overhead | **+2.6 ms p50 / +3.8 ms p95** per request · **+4.25 ms (0.8%)** on a realistic time to first token |
| Concurrent streams, one replica (one core) | Within 10% of a direct connection up to **200** streams; the core saturates at **400** |
| Throughput, 1 → 2 replicas | **445 → 614 req/s** (the test rig tops out at 951) |
| Rate-limit accuracy across 2 replicas | **−0.06%** of the configured limit |
| Budget overshoot, 50 concurrent streams | **+3.4%** (≈2.8 requests) |
| Provider outage | **5** requests went to the dead provider before the breaker opened · **0** errors reached clients |
| Provider down/slow, Redis slow/down, Postgres down | **100%** of requests served in each case (while Redis is down, limits and budgets are off; while Postgres is down, uncached keys get `503`) |
| SIGTERM with open streams | **90/90** streams completed |

To reproduce:

```bash
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build
python tests/load/run_bench.py
python tests/load/report.py
```

## Deploying

The gateway is a stateless container (`Dockerfile`). Run as many replicas as you need
behind a load balancer, with managed Redis and Postgres.

- **Health checks:**
  - `/healthz` is the liveness check.
  - `/readyz` returns `503` until startup finishes and `200` after. It reads a status that a background task refreshes every 5 s, so a probe never waits on a hung database.
  - The `/readyz` body reports Redis and Postgres status, but their failures don't make it fail. Every replica shares those dependencies, so failing readiness would pull all replicas at once, while the gateway can serve without them.
- **Migrations:** the image's default command runs `alembic upgrade head` and then starts the server, which is fine for a single instance. With several replicas, run migrations once per deploy as a job, and start replicas with `uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 30`. `docker-compose.bench.yml` shows this setup.
- **Load balancer:** streams can run for minutes. Set idle and request timeouts above `stream_total` in `models.yaml`, and turn off response buffering for `text/event-stream`.
- **Shutdown:** on `SIGTERM`, uvicorn waits up to `--timeout-graceful-shutdown` for open streams to finish. Give the orchestrator a longer termination grace period than that.
- **Network:** keep `:9100/metrics`, `/admin/*` and ideally `/readyz` (its body names your dependencies) reachable only from inside your network and the load balancer. Pass secrets as environment variables from your secret store.

## FAQ

**Does it work with a ChatGPT Plus or Claude Pro/Max subscription?** No, and that's by design.
The [subscriptions page](docs/wiki/Subscriptions-and-Terms.md) has the per-provider details and sources.
Those subscriptions cover the consumer apps (chatgpt.com, claude.ai, the Claude desktop and
mobile apps, and Claude Code signed in with your account). They don't come with API
credentials, and the providers' terms don't allow their login tokens to be reused to serve
other applications. The gateway uses **API keys**, which are billed per token from the
provider console (console.anthropic.com, platform.openai.com). That billing model is why
per-key budgets and cost tracking matter. A self-hosted model through Ollama costs nothing per
token. The gateway can, however, give *your* users subscription-like plans: tiers in
`config/limits.yaml` (rate limits, monthly budget, allowed models) work as plans, and each key
is a subscriber.

**Can Claude Code or the Anthropic SDK point at it?** Yes, through `/v1/messages` (see
[Anthropic clients](#anthropic-clients-sdk-claude-code)). The gateway key replaces the
Anthropic API key, and requests are billed to the provider API keys configured in the
gateway, not to a subscription.

**Is it only for chat?** For now, yes: `/v1/chat/completions` and `/v1/messages` (both with
tools, images and streaming), plus `/v1/models`. Embeddings and OpenAI's newer Responses API
are not implemented.

## Design decisions

Each non-obvious choice has an ADR in [docs/decisions/](docs/decisions/):

| ADR | Decision |
|-----|----------|
| [0001](docs/decisions/0001-config-driven-models.md) | All models are defined in config, not code |
| [0002](docs/decisions/0002-streaming-errors-and-disconnects.md) | Streaming error semantics and client disconnects |
| [0003](docs/decisions/0003-anthropic-adapter.md) | Anthropic adapter: official SDK, capabilities as config |
| [0004](docs/decisions/0004-retries-fallback-circuit-breaker.md) | Retries, fallback chains and circuit breakers |
| [0005](docs/decisions/0005-mid-stream-failure-policy.md) | No fallback after the first token |
| [0006](docs/decisions/0006-api-keys.md) | Gateway API keys: hashed, cached, stale-if-error |
| [0007](docs/decisions/0007-rate-limits-and-budgets.md) | Token-aware rate limits and budgets: estimate, then reconcile |
| [0008](docs/decisions/0008-observability.md) | Usage log, metrics with bounded labels, logs without prompts |
| [0009](docs/decisions/0009-benchmarking.md) | How the gateway is benchmarked |
| [0010](docs/decisions/0010-anthropic-messages-inbound.md) | Inbound Anthropic Messages API, translated at the edge |

## Development

```bash
make install     # dev dependencies from the hashed lockfile
make test        # unit + integration tests; providers are mocked, so nothing costs money
make test-live   # also hits real providers (costs money)
make lint        # ruff + mypy
make lock        # re-pin after editing requirements*.in (ARGS=--upgrade to bump)
```

The stack is Python 3.14 · FastAPI · httpx · Anthropic SDK · Redis 8 · Postgres 18
(SQLAlchemy async + Alembic) · Prometheus · Grafana. Dependabot opens weekly update PRs.

The repo is set up for [Claude Code](https://claude.com/claude-code): see `CLAUDE.md`,
`.claude/agents/` and `.claude/skills/`.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
