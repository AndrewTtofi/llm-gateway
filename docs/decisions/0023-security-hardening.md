# 0023 — Security hardening after the audit

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
A full security audit of the gateway (at `c75c1a8`) found no auth bypass, injection,
SSRF or secret leak. It did find problems in billing and availability, which matter more
for a gateway shared by several apps and teams:

- **H1. Unbilled usage.** Reasoning output wasn't counted. A request cut off before the
  provider's usage arrived was billed for what had been relayed, often almost nothing.
  Attempts that timed out were refunded, though the provider billed them.
- **H2. One tenant could open a circuit breaker for everyone.** Gateway-side failures
  counted against the shared, fleet-wide breaker, so one key could take a provider away
  from every tenant on every replica:
  - waiting for a free pooled connection;
  - a long generation the client asked for running past the gateway's own time limit.
- **Medium findings:**
  - the token estimate ignored tools, files and tool calls;
  - one oversized value could drop other rows from the usage log;
  - pricing parameters (`service_tier`, search options) passed straight through;
  - huge bodies were parsed before the key was checked;
  - the injection scan only looked at the end of each message;
  - shared caches could be poisoned;
  - spend added during a Redis outage was lost.

## Options considered
For billing a request that was cut off without provider usage:
1. **Bill what was relayed.** This was the old rule. Hanging up early is nearly free, and
   reasoning a provider never streams costs nothing.
2. **Bill the full reservation** (prompt plus `max_tokens`). Safe, but heavy-handed: a
   chat app's "stop" button would cost the whole output limit every time.
3. **Bill what was relayed, but at least the time the provider spent at an assumed
   speed** (default 100 output tokens/s), up to the reservation. Hanging up early stops
   saving money, and an honest early stop costs roughly what the provider charges.

For breaker accounting:
1. **Count every timeout.** Clients can trigger timeouts on a healthy provider.
2. **Count only failures the provider caused.**
   - A full pool isn't counted: the request was never sent.
   - A request that runs past `total` or `stream_total` isn't counted: how long it takes
     depends on what was asked.
   - Connect failures, first-token timeouts, stalled streams and 5xx still count.

## Decision
Billing option 3 and breaker option 2, plus a fix for each other finding.

**Billing (`app/metering.py`, `app/ratelimit`):**
- **What's counted:**
  - Thinking deltas, `reasoning_content` / `reasoning`, and thinking blocks all count as
    output.
  - A request cut off without usage (client hang-up, timeout) is billed at least
    `estimation.output_tokens_per_second` × the seconds it ran, up to its `max_tokens`.
  - Each attempt that timed out after reaching its provider is billed the same way, on
    its own target.
- **Estimate:**
  - It counts tool definitions, every content-part payload (files, audio), tool-call
    arguments and thinking blocks. Base64 payloads count a tenth of their characters: base64
    is about ten times longer than the tokens a provider bills for the file. Without that,
    a cut-off request carrying a PDF would be billed about 30 times too much.
  - When the client sends no `max_tokens`, it uses the first target's `default_max_tokens`
    (Anthropic: 4096 unless configured).
- **Bounds:**
  - `max_tokens` and `max_completion_tokens` are at most 1 000 000.
  - If both are sent, `max_tokens` is dropped, so a rename rule can't make the estimate
    and the request disagree.
  - Usage-log token columns are 64-bit (migration 0007), and values are clamped.
  - A row Postgres rejects costs only that row; only connection failures end a batch.
- **Redis outages:**
  - Spend added while Redis is unreachable is queued per replica and written when Redis is
    back.
  - Budget checks meanwhile use the last known spend plus the queue. Before, they used 0.
  - Delivery is at least once. A flush whose reply was lost, after Redis had applied it, is
    queued again and counted twice. That's the safe side for a budget. A cancelled flush
    keeps its queue.

**Availability (`app/routing`, `app/providers`, `app/streaming.py`, `app/main.py`):**
- **Errors that don't count against the breaker:**
  - `ProviderError.local`: the pool was full. The attempt is `skipped:busy`; if nothing
    else can serve, the client gets a retryable `503 gateway_busy`.
  - `ProviderError.deadline`: the request ran past its total time. Only a read timeout
    counts as this; a stalled upload is a network failure. It isn't retried on the same
    target, though fallback still applies.
  - **Billing a hang-up:** an attempt is "in flight", and billed if the client hangs up,
    only while its call is running. A hang-up during backoff, or after the attempt failed,
    doesn't bill it again.
  - A connect timeout is a network failure: nothing was sent, so nothing is billed.
- **Concurrency limit:**
  - Each tier sets `concurrent_requests`, the number of requests a key may have in flight
    per replica. Over it, the client gets `429 concurrency_limit_exceeded`.
  - It's per replica on purpose, because the connection pools it protects are per replica.
- **Slow readers:** every write to a streaming client gets `CLIENT_WRITE_TIMEOUT_SECONDS`
  (30 s). A client that stops reading is disconnected instead of holding an upstream
  connection.
- **Parsing:**
  - Chat requests check the key before parsing the body.
  - At most 10 000 messages and 1 000 content parts per message.
  - Over-deep JSON gets a 400 on both APIs.
  - The body is serialised once per request.

**Providers (`app/providers/openai_compat.py`):**
- **Held back:** these fields are no longer forwarded to OpenAI-compatible providers
  unless the provider lists them under `params.pass`: `service_tier`, `store`,
  `background`, `metadata`, `web_search_options`, `search_parameters`, `prediction`,
  `audio`, `modalities`. They change the price in ways token pricing doesn't capture, or
  keep tenants' prompts at the provider.
- **`user`:** it's sent as a SHA-256 pseudonym, as the Anthropic path already did, so
  `safety_identifier` on the Responses API is hashed too.

**Guardrails (`app/guardrails.py`):**
- **Long messages:** a message over the per-message budget is scanned at both ends.
- **Tool definitions** are scanned as tool text; their descriptions can come from a
  third-party MCP server.
- **Unscanned text** is counted (`rule="unscanned"`). `unscanned` in guardrails.yaml
  decides what it means:
  - `allow`: nothing;
  - `suspicious` (default): counted, and `flag` tiers get `x-gateway-guardrail: unscanned`.
    It doesn't ask the classifier: that only sees the end of the conversation, which the
    rules already scanned;
  - `block`: tiers set to `injection: block` refuse such requests.

**Cache (`app/cache.py`):**
- **Refresh:** in a team or global scope, `x-gateway-cache: refresh` acts as `bypass`, so
  no caller can overwrite what others get.
- **Shared semantic matching** needs `shared_semantic: true`. It serves one caller's
  answer for another caller's *different* question, so an attacker can plant an answer
  with a near-duplicate prompt.
- **Header hints:** routing hints sent in the `x-gateway-route` header are part of the
  cache key, as body hints already were.

**Other findings:**
- **Metric labels:** names the client typed become metric labels only if the config
  knows them; anything else is `_unknown`.
- **`n`** is validated as an integer from 1 to 128.
- **Teams:** a key whose team was removed from limits.yaml gets `403 team_unknown`
  (fail closed) instead of no team budget.
- **Request ids:**
  - `x-request-id` is always the gateway's own.
  - A caller's id (`x-client-request-id`, or `x-request-id`) is echoed as
    `x-client-request-id` and stored in `usage_log.client_request_id`.
  - So a caller can't reuse someone else's id to confuse joins.
- **Key changes** (revoke, edit) are published on Redis (`gateway:keys-changed`). Every
  replica drops its key cache at once, instead of within 30 s.
- **Read-only role password:** it must be ASCII. PostgreSQL applies SASLprep to non-ASCII
  passwords, which the verifier doesn't.
- **Production deployment:**
  - API docs are off (`DOCS_ENABLED=false`), and Caddy also blocks `/redoc`.
  - Caddy sends HSTS and has header, body and idle timeouts.
  - `migrate` and `prune-usage` get only `DATABASE_URL`.
  - The response cache has its own Redis, capped with LRU eviction.
  - Images are pinned by digest.
  - Dependabot waits 7 days for a release; `httpx2` is pinned directly; there's a
    `.dockerignore`.
- **Alerts:** Redis failing open, stale auth, injection spikes, concurrency rejections.

## Follow-up review (before v1.3.0)
A second review of the request path found five more problems, fixed before the release:
- **`n` wasn't in the estimate.** The output part is now multiplied by `n`, for the
  reservation and for interrupted billing.
- **Check, then reserve, could be outrun.** With real store latency, a burst of 6 requests
  all passed a budget meant for about 2. The reservation is now the check: one Lua script
  per counter (key, then team; the team's hold is undone if the key's is refused).
- **Streams committed on an empty opening chunk** (`message_start`, `response.created`,
  OpenAI's role-only first delta). An overload error right after it couldn't fall back,
  and a stalled stream passed the first-token timeout. The router now holds chunks back
  until one carries output, bounded by `first_output` (default `first_token`).
- **The interrupted-billing cap assumed a limit that was never sent.** Without a client or
  configured `default_max_tokens`, the cap is the model's catalog `max_output_tokens`.
  OpenAI-compatible providers can now send `default_max_tokens`; it's opt-in, because a
  default limit cuts off long answers.
- **Large guardrail scans blocked the event loop** (tens of ms). Prompts over 20 000
  characters are scanned in a worker thread.

## Consequences
- **Bills go up for interrupted requests.** A stream cut off after 10 s is billed at
  least 1 000 output tokens if its limit allows. Operators who meter differently can
  lower `output_tokens_per_second` (0 restores billing what was relayed).
- **First-token timeouts:** they are billed at the time rate too, though often nothing was
  generated (only reasoning models plausibly did). This is conservative, and bounded by
  `first_token` × 100 tokens.
- **Breaker sensitivity:** a provider that hangs only on non-streamed requests is no
  longer taken out by the breaker on that signal. Connect failures, first-token timeouts
  and 5xx still open it.
- **Clients can get new 429s:** a client running many parallel requests on one key can
  now get `concurrency_limit_exceeded`. The defaults: dev 10, standard 50, unlimited for
  load tests.
- **Request ids:** clients that relied on their `x-request-id` coming back as the
  gateway's id now get it in `x-client-request-id`.
- **Still bounded only by config, not removed:**
  - classifier and judge calls are billed to the operator, not the key (bounded by rpm
    and the sample rate);
  - `/v1/catalog` shows tenants which providers are configured.
