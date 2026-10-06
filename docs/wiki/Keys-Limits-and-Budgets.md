# Keys, limits and budgets

ADRs: 0006 (API keys), 0007 (rate limits and budgets), 0023 (hardening: concurrency, billing
interrupted requests, Redis outages).

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
| Hit cache | Valid keys are cached for 30 s. A revoke or edit is published on Redis (`gateway:keys-changed`) and every replica drops its cache at once; if Redis is down, it takes up to 30 s |
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

Shortcut: `make key name=… tier=…`.

**Operators.** Each person who runs the gateway should have their own admin key, so changes
can be traced to them:
- Create one with `make admin-key name=alice`. It prints the key, for Alice, and a line,
  for `ADMIN_KEYS_FILE`.
- The file holds only hashes and is re-read when it changes. Delete the line to remove
  someone.
- Every create, edit, revoke and reload is recorded with the operator's name:

```bash
curl localhost:8000/admin/audit -H "Authorization: Bearer $ALICES_KEY"   # newest first
``` Unknown fields are rejected on create and edit, so a
typo like `monthly_budget` can't silently create a key without a budget.

- **Editing:** name, tier, team, limits and allowed aliases can be changed. Name and tier
  can't be cleared.
- **When it applies:** immediately on every replica (announced through Redis), or within
  30 s (the key cache) if Redis is down.

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
- **Removed teams:** a key whose team is no longer in `limits.yaml` gets `403 team_unknown`.
  It fails closed, so deleting a team can't silently lift its budget.

## Request size

Request bodies above `MAX_BODY_BYTES` (default 32 MiB, Anthropic's request limit) get a
**413** before anything reads them. In production, Caddy enforces the same cap at the edge.

The key is checked **before** the body is parsed, so a caller without a valid key can't
make the gateway spend CPU on a large body. Within the size limit, a request may have at
most 10 000 messages and 1 000 content parts per message, and `max_tokens` at most
1 000 000. Over that, the request gets a 400.

## Tiers

Defined in `config/limits.yaml`. Every key has a tier. Rate limits, budget and allowed
aliases can be overridden per key. Each tier also sets:
- its [prompt-injection](Quality-and-Safety.md#prompt-injection-filter) action
  (`injection: off | log | flag | block`);
- `concurrent_requests`, how many requests one key may have in flight per replica.

| Tier | Requests/min | Tokens/min | Budget/month | In flight | Aliases |
|------|-------------|-----------|--------------|-----------|---------|
| `dev` | 60 | 50 000 | $10 | 10 | `fast`, `local` |
| `standard` | 300 | 200 000 | $100 | 50 | `fast`, `balanced`, `smart`, `local`, `frontier`, `auto` |
| `chaos` | 600 | 1 000 000 | $1 | no limit | chaos aliases (dev only) |

## Concurrency: requests in flight

Rate limits count requests per minute; they don't stop one key from holding many requests
**open** at once. Long streams, read slowly, would tie up the provider connection pool
(100 connections per provider and replica by default) and starve every other tenant.

- **Limit:** a key with `concurrent_requests` requests in flight on a replica gets
  `429 concurrency_limit_exceeded` with `retry-after: 1`. A slot is freed when its request
  settles, however it ends. `0` means no limit.
- **Scope:** per replica on purpose, because the pools it protects are per replica. With
  N replicas, a key can have up to N × the limit in flight.
- **Slow readers:** a streaming client that doesn't take a chunk within
  `CLIENT_WRITE_TIMEOUT_SECONDS` (30 s) is disconnected, and its upstream stream closed.
- **Metric:** `gateway_rejected_total{reason="concurrency"}`, with an alert on spikes.

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
   max_tokens`.
   - **Characters counted:** everything the provider reads, which is message text, tool
     definitions, tool-call arguments, thinking blocks, and file or audio payloads. Base64
     payloads count a tenth of their length, closer to what providers bill.
   - **Images** count a flat 1 000.
   - **No `max_tokens`:** the first target's `default_max_tokens` is used (Anthropic
     requires one: 4096 unless configured), else `default_completion_tokens`.
   - **Both limits sent:** `max_tokens` is dropped and `max_completion_tokens` used, so the
     estimate and what the provider gets always agree.
   - **`n` answers:** the output part is multiplied by `n`. The provider generates, and
     bills, every answer.
2. **After the call**, correct it with the provider's real `usage`: refund an over-estimate,
   or charge the difference. A bucket may go **negative**, which delays the key's next
   request. The debt isn't forgiven by key expiry.

Why not a real tokenizer? `tiktoken` is OpenAI's and miscounts Claude. It also downloads
vocabulary files at runtime. Since reconciliation fixes the count anyway, an exact
pre-count isn't worth it.

The catch: the estimate includes the full `max_tokens`. A client that asks for
`max_tokens: 32000` and uses 500 still reserves 32 000 tokens until it finishes. Claude Code
does exactly this. Give such keys a higher `tokens_per_minute`.

### When the provider reports no usage

Usage arrives at the end: in the last stream chunk, or in the full answer. A request that
is cut off first (the client hangs up, or it times out) has none. The gateway then
estimates, in a way that hanging up early can't game (ADR 0023):

- **Counted output:** everything relayed so far, which is text, tool-call arguments and
  reasoning (thinking blocks, `reasoning_content`).
- **Time floor:** at least `estimation.output_tokens_per_second` (default 100) for every
  second the provider worked on it (times `n`). It's capped by the output limit that was
  *sent* (the client's `max_tokens`, or the provider's `default_max_tokens`). When none
  was sent, it's capped by the model's `max_output_tokens` from the catalog, not the
  estimate's 1 024 default: an unlimited model can write far more than that. Providers keep
  generating, and billing, reasoning they never stream, so counting only what arrived
  would make "hang up just before the answer" nearly free.
- **Timeouts:** each attempt that timed out after reaching its provider is billed the same
  way, on that provider's price, even if a fallback answered in the end. The provider
  billed it.
- **Never reached a provider:** a request that never reached one is fully refunded. That
  covers a connect failure or a full connection pool.

Set `output_tokens_per_second: 0` to bill only what was relayed.

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
  is `billing_error`. The check happens when the request's cost is held, just before
  routing, so a cached answer, which costs nothing, is still served to a key at its budget.
- **Reservation:** the estimated cost, priced at the chain's first target, is added to spend
  immediately, so 50 concurrent requests can't all spend the last dollar. The check ("is
  spend still under the budget?") and the hold are **one atomic step** per counter (a Lua
  script in Redis): a burst can't all pass the same stale check while the store is slow
  (ADR 0023). A request whose hold is refused gets `429 insufficient_quota`.
- **Month:** a request reserves and settles in the month it **started** (UTC). A request
  running across midnight on the 1st doesn't refund into the new month.
- **Settlement:** the reservation is replaced by the **real** cost, priced from
  `config/pricing.yaml` for the target that **actually served**. A fallback may be cheaper or
  more expensive than the first choice.
- **Measured overshoot:** with 50 concurrent streams racing for the last dollar, +0.9%
  (about 0.8 requests). It was +3.4% before holds became atomic.

### Budget alerts

Apps shouldn't find out from a `429` that a budget ran out. When a key or a team reaches
50%, 80% or 100% of its monthly budget (`budget_alerts` in `limits.yaml`), the gateway
sends an alert:
- to the alert webhook (`ALERT_WEBHOOK_URL`, Slack-compatible, the same one breaker alerts
  use);
- as a log line, and as `gateway_budget_alerts_total{level}`.

```json
{"text": "⚠️ LLM gateway: key gw_abc12 (support-bot) has used 80% of its 2026-10 budget ($80.12 of $100.00)",
 "kind": "budget", "who": "key gw_abc12 (support-bot)", "level": 80, "spent_usd": 80.12, "budget_usd": 100}
```

- **When:** checked when a request's cost is held, from the total the reservation returns,
  so it costs nothing extra. Holds count, like for enforcement.
- **How often:** each level alerts once per key or team and month, across all replicas.

ADR 0024.

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
closed, would turn a Redis blip into a total outage of every AI feature.

Spend isn't lost meanwhile (ADR 0023):
- **Queued:** each replica keeps the spend it couldn't write, per key and month, and adds
  it to Redis once Redis answers again.
- **Budget checks:** they use the last spend this replica read plus its queue, not $0, so a
  key that was at its budget stays blocked.

`gateway_redis_fail_open_total{what}` counts calls answered without Redis. The
`GatewayRedisFailingOpen` alert pages on it. If you need strict enforcement (hard spend
caps for a customer), run Redis highly available.
