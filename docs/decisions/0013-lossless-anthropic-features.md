# 0013 — Prompt caching and extended thinking through `/v1/messages`

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 9

## Context
ADR 0010 translates `/v1/messages` into the internal OpenAI format and drops what OpenAI
can't express. Two of the dropped features matter a lot for the main client, Claude Code:

- **Prompt caching (`cache_control`).** An agent re-sends a long, mostly unchanged prompt
  every turn. With caching, the unchanged part is billed at about 10% of the input price
  (cache read) after the first write. Without it, every turn pays full price.
- **Extended thinking.** Claude returns signed thinking blocks. In a tool-using turn, they
  must be sent back unchanged, signature included, for the model to continue.

## Options considered
1. **A separate pass-through path for Anthropic targets.** Lossless, but a second pipeline
   for limits, metering and streaming, and no fallback.
2. **Extension fields in the internal format**, read only by the Anthropic adapter and
   stripped everywhere else.
3. **Make the internal format Anthropic's.** Rejected in ADR 0010.

## Decision
Option 2, defined in `app/extensions.py`:

| Where | Field |
|-------|-------|
| request | `thinking` (the Messages API parameter) |
| content part, string message, tool message, tool definition, assistant tool call | `cache_control` |
| assistant message (history) and response message | `thinking_blocks`: `thinking` (with `signature`) and `redacted_thinking` blocks |
| stream delta | `thinking`: `{index, start}` / `{index, thinking}` / `{index, signature}` |
| usage | `prompt_tokens_details.cache_creation_tokens` (and `cache_creation_1h_tokens`) |

- **Inbound** (`messages_api.py`): the translator keeps `cache_control` on the system
  prompt, parts, tools and tool results. A system prompt or assistant text with a
  breakpoint becomes text parts instead of a joined string.
- **Outbound** (`anthropic_format.py`): the Anthropic adapter restores everything. It sends
  thinking blocks first in the assistant turn, as the API returned them. The system prompt
  is sent as blocks only when it has a breakpoint.
- **Streams:** thinking blocks are streamed as thinking and signature deltas, so the
  Anthropic SDK reassembles them with their signatures.
- **Isolation:**
  - `strip_request` runs in the OpenAI-compatible adapter, before any non-Anthropic
    provider. It also turns text-only part lists on system, developer and assistant
    messages back into strings, since not every OpenAI-compatible server accepts lists
    there.
  - `strip_response` and `strip_chunk` run for `/v1/chat/completions` clients. They remove
    thinking blocks and the cache-write usage fields (the meter reads usage first). A
    thinking-only chunk is dropped entirely.
  - `/v1/chat/completions` requests have `thinking` and `thinking_blocks` removed on the
    way in. Those clients never receive signed thinking blocks back, so they mustn't enable
    thinking: the next tool-use turn would lack the blocks Anthropic requires.
    `cache_control` stays: OpenAI-format clients can use prompt caching with Claude.

## Consequences
- **Caching works:** Claude Code through the gateway gets prompt caching.
- **Billing:** cache writes are billed at their own price (ADR 0015) and reported back as
  `cache_creation_input_tokens`.
- **Thinking works:** tool-using turns keep their signed thinking.
- **Fallback to another model** doesn't break the request:
  - **Non-Claude target:** the extension fields are removed.
  - **Other Claude model:** the thinking blocks are sent anyway, as Anthropic's docs say
    blocks from another model are ignored. If a future model rejects them, the request is
    a 400, which doesn't fall back. That would need stripping on model change.
- **Refusal fallback:** thinking streamed before a refusal fallback boundary is already with
  the client.
- **Not carried:** other Anthropic-only features (citations, document blocks, server
  tools). They are still rejected or dropped as in ADR 0010.
