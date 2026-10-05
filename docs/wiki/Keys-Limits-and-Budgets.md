# Keys, limits and budgets

ADRs: 0006 (API keys), 0007 (rate limits and budgets).

## Gateway API keys

- **Format:** `gw_` + `secrets.token_urlsafe(32)`, which is 256 random bits as 43 URL-safe
  characters.
- **Storage:** the key is shown **once**, at creation. Postgres stores only its SHA-256 hash
  and a short prefix for humans. A database leak doesn't leak usable keys.
- **Hashing:** SHA-256 rather than bcrypt or argon2, because the keys are random with 256
  bits of entropy, so there's nothing to brute-force. A slow hash would only add latency to
  every request.
- **Sending it:** `Authorization: Bearer gw_…` (OpenAI clients) or `x-api-key: gw_…`
  (Anthropic clients). If both are sent, a Bearer token wins. A non-Bearer `Authorization`
  header is ignored.

### The key cache

Looking up Postgres on every request would put the database on the hot path:

| Step | Behaviour |
|------|-----------|
| Format check | A string that doesn't look like a key is rejected without any lookup |
| Hit cache | Valid keys are cached for 30 s, so revocation takes effect within 30 s on other replicas and immediately on the one that revoked |
| Miss cache | Unknown keys are cached too, so random keys can't hammer Postgres |
| Coalescing | Concurrent lookups of the same key share one query |
| Stale-if-error | If Postgres is down, a key seen in the last 10 min keeps working (`gateway_auth_stale_served_total`) |
| Unknown key + Postgres down | 503 `auth_unavailable`: fail closed |

### Managing keys

```bash
# create (the key is in the response, once)
curl -X POST localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"support-bot","tier":"standard","tokens_per_minute":100000,"monthly_budget_usd":50}'

# list, with month-to-date spend
curl localhost:8000/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"

# edit in place: the app keeps the same key (null clears an override back to the tier's value)
curl -X PATCH localhost:8000/admin/keys/<id> -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H 'content-type: application/json' -d '{"tokens_per_minute":250000,"team":"support","monthly_budget_usd":null}'

# revoke
curl -X DELETE localhost:8000/admin/keys/<id> -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```

Shortcut: `make key name=… tier=…`. Unknown fields are rejected on create and edit, so a
typo like `monthly_budget` can't silently create a key without a budget.

- **Editing:** name, tier, team, limits and allowed aliases can be changed. Name and tier
  can't be cleared.
- **When it applies:** an edit applies immediately on the replica that handled it, and
  within 30 s (the key cache) on the others.

## Teams

A key can belong to a team. Teams are declared in `limits.yaml` with their own monthly
budget, and a typo'd team name is rejected:

```yaml
teams:
  support: { monthly_budget_usd: 500 }
  data:    { monthly_budget_usd: 200 }
```

- **Budgets:** a request must fit **both** budgets, the key's and its team's. When the team
  budget is used up, every key in the team gets `429 insufficient_quota` ("…team is
  exhausted"). The rejection metric's reason is `team_budget`.
- **Spend tracking:** spend is reserved and settled for the key and the team together.
  `usage_log.team` records the team at request time, so moving a key later doesn't rewrite
  history. This month's spend also stays with the old team.
- **Where to see it:**
  - `GET /admin/teams` lists budget, month-to-date spend and active keys per team.
  - Grafana has "Spend per team" panels.
- **Overshoot:** many keys in one team racing for the last dollar can overshoot its budget
  by about the cost of the requests in flight at that moment (ADR 0016).

## Request size

Request bodies above `MAX_BODY_BYTES` (default 32 MiB, Anthropic's request limit) get a
**413** before anything reads them. In production, Caddy enforces the same cap at the edge.

## Tiers

Defined in `config/limits.yaml`. Every key has a tier, and any field can be overridden per key.
Each tier also sets its [prompt-injection](Quality-and-Safety.md#prompt-injection-filter)
action (`injection: off | log | flag | block`).

| Tier | Requests/min | Tokens/min | Budget/month | Aliases |
|------|-------------|-----------|--------------|---------|
| `dev` | 60 | 50 000 | $10 | `fast`, `local` |
| `standard` | 300 | 200 000 | $100 | `fast`, `balanced`, `smart`, `local`, `frontier`, `auto` |
| `chaos` | 600 | 1 000 000 | $1 | chaos aliases (dev only) |

`allowed_aliases` can also list exact `provider/model` names, or `"*"` for everything.

## Rate limits: two token buckets

Each key has two buckets, **requests per minute** and **tokens per minute**, checked and
charged **together** by one Redis Lua script. "Check, then charge" as two separate commands
would let concurrent requests on different replicas both pass the check and overdraw.

- **Capacity:** a bucket holds one minute's allowance. A client can burst its whole
  per-minute budget, then gets the steady refill rate.
- **Refill:** continuous, not reset on the minute.
- **Clock:** Redis `TIME`, so replicas don't need synchronised clocks.
- **Measured accuracy** across 2 replicas: −0.06% of the configured limit.

### Estimate, then reconcile

Tokens aren't known until the provider answers, so:

1. **Before the call**, charge an estimate: `prompt characters ÷ chars_per_token +
   max_tokens`. When the client sends no `max_tokens`, `default_completion_tokens` is used.
   Images count a flat 1 000.
2. **After the call**, correct it with the provider's real `usage`: refund an over-estimate,
   or charge the difference. A bucket may go **negative**, which delays the key's next
   request. The debt isn't forgiven by key expiry.

Why not a real tokenizer? `tiktoken` is OpenAI's and miscounts Claude. It also downloads
vocabulary files at runtime. Since reconciliation fixes the count anyway, an exact
pre-count isn't worth it.

The catch: the estimate includes the full `max_tokens`. A client that asks for
`max_tokens: 32000` and uses 500 still reserves 32 000 tokens until it finishes. Claude Code
does exactly this. Give such keys a higher `tokens_per_minute`.

### Responses

- **Every response** carries OpenAI-style headers:
  `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`,
  `x-ratelimit-reset-requests`, and the same three for `-tokens`.
- **Over a limit:** `429 rate_limit_exceeded` with `retry-after` (seconds until both buckets
  have room). The OpenAI and Anthropic SDKs honour it automatically.

## Budgets

- **Tracking:** month-to-date spend per key is kept in Redis (`spend:{key}:{YYYY-MM}`, UTC,
  expiring after about 2 months).
- **Admission:** a request is refused if spend is already at or over the budget, with
  `429 insufficient_quota`, the error OpenAI uses for an empty account. On `/v1/messages` it
  is `billing_error`.
- **Reservation:** the estimated cost, priced at the chain's first target, is added to spend
  immediately, so 50 concurrent requests can't all spend the last dollar.
- **Settlement:** the reservation is replaced by the **real** cost, priced from
  `config/pricing.yaml` for the target that **actually served**. A fallback may be cheaper or
  more expensive than the first choice.
- **Measured overshoot:** with 50 concurrent streams racing for the last dollar, +3.4%
  (about 2.8 requests).

### How cost is calculated

```
cost = (prompt − cache reads − cache writes) × input
     + cache reads             × cached_input
     + 5-minute cache writes   × cache_write
     + 1-hour cache writes     × cache_write_1h
     + completion              × output                   (prices per 1M tokens)
```

Three refinements (ADR 0015):

- **Tiers:** if the prompt is larger than a tier's `above_prompt_tokens`, that tier's prices
  apply to the **whole** request. This is how OpenAI (above 272K tokens), Gemini and xAI
  (above 200K) bill.
- **Off-peak:** if the model has `off_peak` windows, a request that *starts* outside them is
  multiplied by `multiplier`. This is DeepSeek's half price outside peak hours.
- **Missing prices:** a missing `cached_input` bills cache reads as normal input. A missing
  `cache_write` bills cache writes as normal input. For Anthropic's refusal fallback, every attempt in `usage.iterations`
is billed at its own model's price. Models missing from `pricing.yaml` log a warning once.
They are recorded with cost `null` and count as $0 against budgets, so price every model you
put in a chain.

## When Redis is down

Rate limits and budgets **fail open**: requests are allowed, the outage is logged when it
starts and ends, and Redis is skipped for 5 s after an error. The alternative, failing
closed, would turn a Redis blip into a total outage of every AI feature. If you need strict
enforcement (hard spend caps for a customer), run Redis highly available. Alerting on Redis
errors matters more than the fail-open setting itself.
