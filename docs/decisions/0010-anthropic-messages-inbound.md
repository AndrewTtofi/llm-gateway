# 0010 — Inbound Anthropic Messages API (`/v1/messages`)

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 8

## Context
Until now clients had to speak OpenAI chat completions. The Anthropic SDKs and Claude Code
speak the Anthropic Messages API (`POST /v1/messages`, `x-api-key`, named SSE events), so
they couldn't use the gateway. Teams using them would bypass the gateway's keys, limits,
budgets, fallback and cost tracking.

The constraints:

- The internal format is OpenAI chat completions (CLAUDE.md). Routing, metering, token
  estimates and every adapter depend on it.
- Errors to clients use OpenAI's shape (CLAUDE.md), but Anthropic SDKs only map Anthropic's
  shape (`{"type":"error","error":{"type","message"}}`) to typed exceptions.

## Options considered
1. **Translate at the edge.** Messages request → internal OpenAI request → normal pipeline
   → OpenAI result → Anthropic message or events.
   - Pros: one pipeline. A Messages request can fall back to OpenAI or Ollama. Limits,
     budgets, breakers, metrics and usage rows work unchanged.
   - Cons: lossy for features OpenAI's format can't express, and a Claude target is
     translated twice (Anthropic → OpenAI → Anthropic).
2. **Passthrough to Anthropic.** Forward the raw body to Anthropic targets only.
   - Pros: lossless, including prompt caching and thinking.
   - Cons: no fallback to other providers, and a second pipeline for auth, limits, metering
     and streaming accounting. Routing would depend on the client's format.
3. **Make the internal format Anthropic's.**
   - Cons: rewrites every adapter and the metering, for one client type.

## Decision
Option 1: translate at the edge (`app/messages_api.py`). This keeps one pipeline, and
fallback across providers, which is the gateway's purpose, works for these clients too.

What crosses the boundary:

- **Requests:**
  - system prompt, including system messages inside `messages`;
  - text and images;
  - `tool_use` and `tool_result`. Tool results become `tool` messages, placed straight
    after the tool calls. Images inside tool results move to a following user turn.
  - custom tools and `tool_choice` (auto / any / tool / none, `disable_parallel_tool_use`);
  - `stop_sequences`, `temperature`, `top_p`, `metadata.user_id`;
  - `output_config.effort` → `reasoning_effort`.
- **Responses:**
  - text and `tool_use` blocks;
  - stop reasons;
  - usage, with cache reads split back out of `prompt_tokens`.
- **Streams:** `MessagesStream` turns flat OpenAI deltas into Anthropic's numbered content
  blocks (`content_block_start` / `_delta` / `_stop`).
  - Tool-call blocks stay open until the tool calls end and then all close together.
    Agents act on `content_block_stop`, and some providers interleave the arguments of
    parallel calls.
  - A different tool-call id at the same OpenAI index is a new call.
  - `message_start` carries the gateway's input-token estimate. `message_delta` carries the
    stop reason and the provider's usage, or the meter's estimates if the provider sent none.
  - A mid-stream failure is an `event: error` with no `message_stop`, the same rule as
    ADR 0002 and ADR 0005.
- **Errors:** on `/v1/messages` and `/v1/messages/*`, errors use Anthropic's shape and
  types, keyed by HTTP status. This is an exception to the OpenAI-shape rule, because the
  clients only understand Anthropic's shape. A budget-exhausted 429 has type
  `billing_error`, so clients can tell it apart from a rate limit that's worth retrying.
- **Auth:** `x-api-key` is accepted alongside `Authorization: Bearer` on every endpoint.
  Both carry the same gateway key. If both are sent, a Bearer token wins. A non-Bearer
  `Authorization` header is ignored.
- **`metadata.user_id`** is forwarded as a SHA-256 hash, so it's opaque and fixed-length
  for every provider.
- **Logging:** rejections log a fixed field name (`InboundError.field`), never the message,
  which may quote client input.
- **`/v1/messages/count_tokens`:** returns the gateway's own estimate (the one rate limits
  use), not a provider's exact count, because the serving provider isn't known in advance.
  Each call counts as one request against the key's requests/min, and the model must be
  allowed for the key.

Things that can't be expressed are handled two ways:

- **Dropped silently:** extended `thinking` (in requests and history), `top_k`,
  `cache_control`, and effort values other than low, medium, high and xhigh. The answer is
  still correct without them.
- **Lossy:**
  - A matched stop sequence is reported as `end_turn` with `stop_sequence: null`.
  - `is_error` on a tool result becomes a `[tool error]` text prefix.
  - Malformed tool arguments are streamed as-is, but in a non-streamed response they're
    wrapped as `{"_raw_arguments": …}`.
- **Rejected with a 400:** document blocks, server tools (web search, code execution…) and
  unknown block types. Dropping these would change the answer.

## Consequences
- The Anthropic SDKs and Claude Code work by setting the base URL to the gateway and using
  a gateway key. This was checked with the real SDK in tests and with Claude Code against
  the fake provider.
- **No prompt caching through `/v1/messages` yet.** `cache_control` is dropped, so long
  agent sessions (Claude Code) pay full input price on Claude targets.
- **No extended thinking through `/v1/messages` yet.** Thinking blocks aren't returned.
- The natural next step for both is a lossless path for Anthropic targets: carry
  `cache_control` and `thinking` through the internal format as Anthropic-specific
  extension fields. Other adapters would ignore them.
- `output_config.effort` becomes `reasoning_effort` for every target in the chain. A
  fallback to a model that rejects it may fail with a 400, which stops the chain. The same
  is true of `/v1/chat/completions` clients that send `reasoning_effort`. Gating it on
  target capabilities in the OpenAI-compatible adapter is a follow-up.
- OpenAI accepts at most 4 stop sequences; a longer list fails on OpenAI targets.
- Clients like Claude Code send a large `max_tokens` (e.g. 32 000). The up-front token
  estimate (ADR 0007) reserves that much of the tokens-per-minute bucket until the request
  reconciles, so those keys need a tier with a larger `tokens_per_minute`.
