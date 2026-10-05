# 0007 — Token-aware rate limits and budgets

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 4

## Context
Requests to an LLM differ in cost by orders of magnitude: "hi" and a 100-page document
are both one request. Limiting requests alone doesn't protect provider quotas or
budgets; limits must also count **tokens** — but the token count is only known *after*
the response.

## Decision
**Two token buckets per key** (requests/min and tokens/min), checked and charged
together by one Redis Lua script so concurrent requests on several gateway instances
can't overdraw them. A bucket holds up to one minute's allowance (a client may burst
its whole per-minute budget, then gets the refill rate) and refills continuously. Redis
`TIME` is the clock, so instances don't need synchronised clocks.

**Estimate, then reconcile.** Before the call the token bucket is charged an estimate:
`prompt characters / 4 + requested max_tokens` (or a configured default). After the
call the real `usage` corrects it — refunding over-estimates, or charging the
difference (a bucket may go negative, which delays the next request; the Redis key
lives until the debt is repaid, so debt is never forgiven by expiry). No tokenizer:
tiktoken is OpenAI's and miscounts Claude, it downloads BPE files at runtime, and
reconciliation makes an exact pre-count unnecessary. The plan listed tiktoken; it's
removed from the dependencies.

**Usage for streams.** The gateway always asks providers for streamed usage
(`stream_options.include_usage`) and forwards the usage chunk only if the client asked
for it. If no usage arrives (disconnect, provider without support), relayed characters
/ 4 is used.

**429s** carry `retry-after` (seconds until both buckets have room) and
`x-ratelimit-{limit,remaining,reset}-{requests,tokens}` — the headers OpenAI's SDKs and
dashboards already understand. Successful responses carry them too.

**Budgets:** month-to-date spend per key in Redis (`spend:{key}:{YYYY-MM}`, UTC,
expires after ~2 months). Same estimate-then-reconcile pattern as tokens: at admission
the estimated cost (priced at the chain's first target) is **reserved**; at the end it
is replaced by the real cost, priced from `config/pricing.yaml` for the target that
actually served (a fallback may be cheaper or dearer). With an Anthropic refusal
fallback, every attempt in `usage.iterations` is billed at its own model's price. A key
at or over its budget gets 429 `insufficient_quota` — what OpenAI returns, so clients
already handle it.

**Disconnects aren't free.** If the client hangs up before the answer, the provider has
already processed (and billed) the prompt, so the estimated prompt is charged to the
target that was working on it. A stream cut mid-way is charged for what was relayed.

**Failure mode:** if Redis is down, limits and budgets **fail open** (like the breaker):
availability over strict enforcement, logged when an outage starts and ends.

## Consequences
- Admission checks `spent (incl. reservations) < budget`, so a budget is overshot by at
  most the gap between estimate and actual of requests in flight — not by every
  request admitted while earlier ones were still running.
- Redis is the budget's source of truth until Phase 5's Postgres usage log exists;
  losing Redis data resets month-to-date spend. Phase 5 can rebuild it from the log.
- Models with `null` prices cost 0 for budgeting (logged) — fill `pricing.yaml`.
- Cached-token discounts aren't priced yet (Phase 5).
