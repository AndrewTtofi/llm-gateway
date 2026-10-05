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
  blocks (`content_block_start` / `_delta` / `_stop`). `message_delta` carries the stop
  reason and usage at the end. A mid-stream failure is an `event: error` with no
  `message_stop`, the same rule as ADR 0002 and ADR 0005.
- **Errors:** on `/v1/messages*`, errors use Anthropic's shape and types, keyed by HTTP
  status. This is an exception to the OpenAI-shape rule, because the clients only
  understand Anthropic's shape.
- **Auth:** `x-api-key` is accepted alongside `Authorization: Bearer` on every endpoint.
  Both carry the same gateway key.
- **`/v1/messages/count_tokens`:** returns the gateway's own estimate (the one rate limits
  use), not a provider's exact count, because the serving provider isn't known in advance.

Things that can't be expressed are handled two ways:

- **Dropped silently:** extended `thinking` (in requests and history), `top_k`,
  `cache_control`, and which stop sequence matched. The answer is still correct without them.
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
- Clients like Claude Code send a large `max_tokens` (e.g. 32 000). The up-front token
  estimate (ADR 0007) reserves that much of the tokens-per-minute bucket until the request
  reconciles, so those keys need a tier with a larger `tokens_per_minute`.
