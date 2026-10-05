# Use case: one gateway for many apps

This walkthrough sets the gateway up as the **single point of contact** for every LLM call in
a small company. Several apps each get their own key, limits and budget. The platform team
owns providers, models and spend in one place.

## The situation

A company runs five things that call LLMs:

| App | What it does | Needs |
|-----|--------------|-------|
| **support-bot** | Customer-facing chat on the website | Fast, always available, streaming |
| **docs-qa** | Internal Q&A over company docs (RAG) | Good quality, moderate volume |
| **nightly-digest** | Summarises the day's tickets at 02:00 | Cheap, high volume, not latency-sensitive |
| **sales-assist** | Drafts emails inside the CRM; uses tools | Best quality, low volume |
| **developers** | Engineers using Claude Code on company billing | Large contexts, per-person spend visibility |

Without a gateway, each app has its own provider keys, SDK quirks and retry logic. Nobody can
say which app spent what, and an OpenAI or Anthropic outage takes down whichever apps
depended on it.

With the gateway:

```
support-bot ─┐
docs-qa ─────┤                      ┌─► Anthropic (Claude)
nightly-dig. ├──► LLM Gateway ──────┼─► OpenAI
sales-assist ┤   one URL            └─► Ollama (on-prem GPU box)
developers ──┘   one key per app
```

- **Provider keys** live only in the gateway's secret store.
- **Each app** has one gateway key.
- **Models** are chosen by alias, so the platform team can change them without touching apps.

## Step 1: aliases by purpose, not by model

Name aliases after **what the app needs**, not which model serves it. Then a model change is
one YAML edit.

```yaml
# config/models.yaml
aliases:
  chat-fast:            # support-bot: low latency, three providers deep
    chain:
      - anthropic/claude-haiku-4-5-20251001
      - openai/gpt-6-luna
      - ollama/llama3.2:3b
  qa:                   # docs-qa: balanced quality and cost
    chain:
      - anthropic/claude-sonnet-5-5
      - openai/gpt-6.1-sol
  bulk:                 # nightly-digest: cheapest first, free last resort
    chain:
      - openai/gpt-6-luna
      - anthropic/claude-haiku-4-5-20251001
      - ollama/llama3.2:3b
  best:                 # sales-assist: highest quality
    chain:
      - anthropic/claude-opus-5-5
      - anthropic/claude-sonnet-5-5
      - openai/gpt-6-astra
  coding:               # developers via Claude Code
    chain:
      - anthropic/claude-sonnet-5-5
      - anthropic/claude-opus-5-5
```

Every target needs a price in `config/pricing.yaml`, or its spend counts as $0.

## Step 2: tiers by app class

```yaml
# config/limits.yaml
tiers:
  customer-facing:
    requests_per_minute: 1200
    tokens_per_minute: 1500000
    monthly_budget_usd: 2000
    allowed_aliases: [chat-fast]
  internal:
    requests_per_minute: 300
    tokens_per_minute: 400000
    monthly_budget_usd: 300
    allowed_aliases: [qa, best]
  batch:
    requests_per_minute: 600
    tokens_per_minute: 600000       # bounded so it can't starve the chatbot upstream
    monthly_budget_usd: 150
    allowed_aliases: [bulk]
  developer:
    requests_per_minute: 120
    tokens_per_minute: 600000       # Claude Code reserves ~32k max_tokens per request
    monthly_budget_usd: 200
    allowed_aliases: [coding]
```

`make reload` applies both files without a restart.

## Step 3: one key per app and environment

```bash
admin() { curl -s -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" -H 'content-type: application/json' "$@"; }

admin -X POST localhost:8000/admin/keys -d '{"name":"support-bot-prod","tier":"customer-facing"}'
admin -X POST localhost:8000/admin/keys -d '{"name":"support-bot-staging","tier":"customer-facing","monthly_budget_usd":50}'
admin -X POST localhost:8000/admin/keys -d '{"name":"docs-qa-prod","tier":"internal"}'
admin -X POST localhost:8000/admin/keys -d '{"name":"sales-assist-prod","tier":"internal","allowed_aliases":["best"],"monthly_budget_usd":100}'
admin -X POST localhost:8000/admin/keys -d '{"name":"nightly-digest","tier":"batch"}'
admin -X POST localhost:8000/admin/keys -d '{"name":"dev-alice","tier":"developer"}'
admin -X POST localhost:8000/admin/keys -d '{"name":"dev-bob","tier":"developer"}'
```

Each response contains the key once. Put it straight into that app's secret store.

Separate keys per environment mean a staging load test can't eat the production budget.
Separate keys per developer make spend visible per person.

## Step 4: point each app at the gateway

The apps only change their base URL and key.

**support-bot** (Python, OpenAI SDK, streaming):

```python
from openai import OpenAI
llm = OpenAI(base_url="https://llm.internal.example.com/v1", api_key=os.environ["LLM_GATEWAY_KEY"])
stream = llm.chat.completions.create(model="chat-fast", messages=history, stream=True)
```

**docs-qa** (LangChain):

```python
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(base_url="https://llm.internal.example.com/v1",
                 api_key=os.environ["LLM_GATEWAY_KEY"], model="qa")
```

**sales-assist** (TypeScript, Anthropic SDK, with tools):

```ts
import Anthropic from "@anthropic-ai/sdk";
const llm = new Anthropic({ baseURL: "https://llm.internal.example.com", apiKey: process.env.LLM_GATEWAY_KEY });
const msg = await llm.messages.create({ model: "best", max_tokens: 1024, tools, messages });
```

**nightly-digest** (any HTTP client): call `/v1/chat/completions` with `model: "bulk"`. Honour
`retry-after` on 429, and the job paces itself to its tier.

**developers** (Claude Code):

```bash
export ANTHROPIC_BASE_URL=https://llm.internal.example.com
export ANTHROPIC_AUTH_TOKEN=<dev-alice key>
export ANTHROPIC_MODEL=coding
```

## Step 5: run it

**Spend.** Grafana → *LLM Gateway* → "Spend per key — this month" shows every app side by
side. For finance, query the usage log directly:

```sql
SELECT k.name AS app, count(*) AS requests,
       sum(u.prompt_tokens) AS input_tokens, sum(u.completion_tokens) AS output_tokens,
       round(sum(u.cost_usd)::numeric, 2) AS usd
FROM usage_log u JOIN api_keys k ON k.id = u.key_id
WHERE u.created_at >= date_trunc('month', now())
GROUP BY k.name ORDER BY usd DESC;
```

**Budgets.**
- When `nightly-digest` hits $150, it gets `429 insufficient_quota` and stops. The support
  bot is unaffected.
- Raising a budget is one call. Keys can't be edited in place yet, so create a new key with
  the higher budget and revoke the old one, or change the tier in `limits.yaml` and reload.

**An outage.** Anthropic returns 529 (overloaded) for Haiku:

1. The first few support-bot requests retry, then fall back to `openai/gpt-6-luna`. Users
   see answers, slightly different in style, and no errors.
2. After 5 failures in 60 s, Haiku's breaker opens. Requests skip it immediately, so there's
   no added latency.
3. Every 30 s one probe checks Haiku. When it succeeds, traffic returns.
4. Grafana shows the fallback rate for `chat-fast` rising and the breaker timeline going red.
   The `GatewayCircuitOpen` alert fires.

`docs-qa` (Sonnet) and `coding` keep working, because breakers are per model, not per provider.

**Changing models.** A new, cheaper model is released, and you want to try it for the
digest:

1. Add it to `models.yaml` and `pricing.yaml` and put it first in `bulk`.
2. Run `make reload`.
3. The next nightly run uses it, with no app deploy.
4. Compare cost and latency per target in Grafana. If the quality isn't good enough, revert
   the YAML.

**Onboarding a new app.** Pick or create an alias and a tier, issue a key, and share the base
URL. The app needs no provider accounts, no provider SDK and no retry logic of its own.

**Offboarding or a leaked key.** `DELETE /admin/keys/{id}`. It takes effect within 30 s
everywhere, and the provider keys never need rotating, because apps never had them.

## What each party gets

| Who | Gets |
|-----|------|
| App developers | One URL, one key, any SDK; failover and retries for free; no provider accounts |
| Platform team | One place for provider keys, models, limits and monitoring; model changes without app deploys |
| Finance | Exact spend per app, team and person; hard monthly caps |
| Security | Provider keys in one service; prompts never logged; per-app access to models; instant revocation |
| Users | Fewer outages: one provider's incident becomes a fallback, not a failure |

## Going further

- **Team budgets:** put each app's keys in a team (`limits.yaml` teams, `team` on the key)
  for a per-team monthly cap and spend panel ([Keys, limits and budgets](Keys-Limits-and-Budgets.md#teams)).
- **Edit keys:** change a key's limits in place with `PATCH /admin/keys/{id}`. The app keeps
  its key.
- **Let the gateway choose:** `nightly-digest` could use `model: auto` with
  `optimize: cost`. The cheapest model that fits each request wins ([Smart routing](Smart-Routing.md)).
- **Test changes safely:** try a cheaper model for 10% of `support-bot` traffic as an A/B
  arm. Then compare cost, latency and judge scores per arm before switching.
- **Cache the FAQ:** a semantic cache on the support bot's alias answers repeated questions
  for free ([Response cache](Response-Cache.md)).
- **Watch for injection:** set the customer-facing tier's `injection` to `flag`, then
  `block` ([Quality and safety](Quality-and-Safety.md)).
