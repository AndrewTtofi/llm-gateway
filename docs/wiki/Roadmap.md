# Roadmap

Where the gateway goes after v1.3.0. Phases 0–11 are done (see `PLAN.md` and the
[CHANGELOG](https://github.com/AndrewTtofi/llm-gateway/blob/main/CHANGELOG.md)). This page is
a plan, not a promise. Items come from gaps found while building and auditing the gateway:
- decisions deferred in the ADRs;
- requests the gateway still rejects;
- what running it for real will need.

Each item says **why** it matters, so the order can change as needs do.

## Next: run it for real (v1.4)

The gateway is built, tested and hardened, but it hasn't served real traffic yet.

| Item | Why |
|------|-----|
| **Pick a deploy target and deploy**: a VM with `docker-compose.prod.yml`, Cloud Run or ECS | Everything so far is verified locally. Real traffic shows what the benchmarks can't |
| **Live tests for every provider in the chains** (`OPENAI_API_KEY` and the rest) | The OpenAI fallbacks and the Responses API path have only been tested against mocks |
| **Budget alerts at 50 / 80 / 100%** through the alert webhook | Today a key or team finds out its budget ran out from a 429 |
| **Dashboards from real usage**: tune guardrail weights, cache thresholds, `concurrent_requests` and `output_tokens_per_second` | All of these defaults are educated guesses until real traffic tunes them |
| **Backups and a restore drill** for Postgres (keys, usage log) | The usage log is the finance record |

## Soon: wider API coverage (v1.5)

| Item | Why |
|------|-----|
| **`/v1/responses` inbound** (the OpenAI Responses API for clients) | OpenAI's newer SDK features and agent tools speak it. The gateway only uses it outbound today |
| **`/v1/embeddings`** through aliases, with limits and cost tracking | Apps doing retrieval call embeddings constantly; today they bypass the gateway |
| **Anthropic server tools and document blocks** on `/v1/messages` (rejected with 400 now) | Web search, code execution and PDF documents are common in Claude apps |
| **Batch APIs** (Anthropic and OpenAI, ~50% cheaper) for jobs that can wait | The cheapest tokens are the ones billed at half price |
| **Exact token counts** from the providers' count endpoints, for `count_tokens` and the estimate | The character estimate is rough. Billing is reconciled anyway, but rate limits and context fit use the estimate |

## Later: operating it at scale (v2)

| Item | Why |
|------|-----|
| **Kubernetes**: a Helm chart, HPA on in-flight requests, PodDisruptionBudgets | The compose file shows the pieces; most production platforms are Kubernetes |
| **OpenTelemetry tracing** (gateway → provider spans, trace ids into the usage log) | Metrics say *that* p95 rose; traces say *where* |
| **Fleet-wide concurrency limits** (in Redis), as an option | The per-key limit is per replica today (ADR 0023) |
| **Redis Cluster / Sentinel, and multi-region** | Limits fail open when Redis is down; high availability makes that rare |
| **An admin UI and self-service keys**, with SSO for operators | Today keys, teams and budgets are managed with curl and the admin key |
| **Per-team routing defaults** (policy, allowed models, guardrail level per team) | Teams have budgets; they should be able to have their own rules too |
| **Bill classifier and judge calls to the key** that caused them | They're the operator's cost today (ADR 0023) |

## Ideas: quality and safety

| Item | Why |
|------|-----|
| **PII detection and redaction** before prompts leave the gateway | The gateway sees every prompt; it's the natural place to keep emails, card numbers and IDs from providers |
| **Output scanning** (secrets, PII, unsafe links in answers) | The injection filter only looks at what goes in |
| **Offline evaluation sets** next to judge sampling | Regression-test a model or prompt change before an A/B test exposes users to it |
| **Prompt templates and versions** managed in the gateway | A/B tests already change prompts per arm; versioned templates make that reviewable |

## Not planned

- **Routing through consumer subscriptions** (Claude Pro/Max, ChatGPT Plus, Copilot). Their
  terms forbid serving them to other users or apps
  ([Subscriptions and terms](Subscriptions-and-Terms.md)).
- **Hard-coding models or prices in code.** Config stays the only place models are named.

## How items get done

Same as every phase so far:
1. Plan the work, and write an ADR for any non-obvious choice.
2. Do it on its own branch, with tests (mocked providers; live tests marked and opt-in).
3. Run a review.
4. Update the docs in the same PR. The wiki syncs itself after the merge.

To propose an item, open an issue or a PR against this page.
