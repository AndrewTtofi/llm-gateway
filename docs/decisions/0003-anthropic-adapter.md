# 0003 — Anthropic adapter: official SDK, config-driven model capabilities

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 2

## Context
Clients speak OpenAI chat-completions; Claude speaks the Messages API. The two differ in
where the system prompt lives, how tool calls/results are represented, required fields
(`max_tokens`), stop reasons, usage accounting and stream events. Current Claude models
also differ from each other: newer ones reject `temperature`/`top_p` and forced
`tool_choice` (400), and accept `effort`; Haiku 4.5 is the reverse.

## Options considered
1. **Raw httpx, like the OpenAI adapter** — one HTTP library, `respx` mocks work; but we
   own SSE parsing, auth/version headers and error typing for an API that changes often.
2. **Official `anthropic` SDK** — maintained event types, headers, typed errors, beta
   params (`betas`, `fallbacks`) as named arguments. Cost: it's built on `httpx2`, so
   tests mock with `httpx2.MockTransport` instead of `respx`.
3. **A generic translation library (LiteLLM etc.)** — fast, but hides exactly the
   translation this project exists to teach and own.

Per-model differences:
a. `if model.startswith(...)` in code — violates "no model names in `app/`".
b. **Capability flags per model in `config/models.yaml`** (`sampling`,
   `forced_tool_choice`, `effort`, `refusal_fallback`) with provider-level `defaults`.

## Decision
Option 2 with (b). SDK retries are disabled (`max_retries=0`) — retries and fallback
are gateway policy (Phase 3). Translation lives in pure functions
(`anthropic_format.py`) so each rule has a unit test.

Translation choices worth knowing:
- `system`/`developer` messages anywhere → one top-level `system` (a mid-conversation
  system message moves to the front).
- Consecutive `tool` messages → one user message with several `tool_result` blocks
  (splitting them teaches the model to stop calling tools in parallel).
- Forced `tool_choice` on a model that rejects it → `auto` + a system instruction
  naming the tool. A strong nudge, not a guarantee.
- `temperature` clamped to Anthropic's 0–1; dropped where the model rejects it.
- `response_format: json_schema` → `output_config.format`; `json_object` → instruction.
- Unknown OpenAI params (`seed`, `frequency_penalty`, `logprobs`, …) are dropped; `n>1`
  and unsupported content parts are a 400.
- Thinking blocks are dropped from responses (no OpenAI equivalent). Usage:
  `prompt_tokens` = input + cache read + cache write, because OpenAI's count includes
  cached tokens and Anthropic's doesn't.
- Streaming tool definitions get `eager_input_streaming` so arguments stream like
  OpenAI's instead of arriving in one burst.
- Models flagged `refusal_fallback` use the beta server-side refusal fallback
  (`fallbacks: "default"`): if a safety classifier declines, the API retries on a
  fallback model inside the same call. This is separate from the gateway's own
  outage fallback chain (Phase 3).

Added after code review (each has a test):
- The Messages API needs a user turn first: a system-only request or a conversation
  opening with a seeded assistant greeting gets a minimal `(start)` user turn prepended.
- `temperature` and `top_p` together are rejected by Claude 4+, so `temperature` wins.
- JSON schemas (`response_format` and `strict` tools) go through the SDK's
  `transform_schema` (adds `additionalProperties: false`, moves unsupported
  `minLength`/`minimum`-style constraints into descriptions).
- `user` is sent as a SHA-256 hash in `metadata.user_id` — clients put emails there.
- `image/jpg` is normalised to `image/jpeg`; non-image data URLs are a 400.
- Malformed input (missing keys, wrong types) is a 400, not a 500.
- Streaming: a no-argument tool call emits `"{}"` (OpenAI does; `""` breaks
  `json.loads`). On refusal-fallback models tool calls are buffered until the message
  completes, because a model that declines mid-stream may have started a tool call
  that must be discarded at the `fallback` boundary. Those models lose incremental
  tool-argument streaming; text still streams.
- `usage.iterations` (per-attempt usage when a fallback ran) is passed through on the
  OpenAI usage object for Phase 5 cost tracking — top-level usage covers only the
  attempt that produced the answer, and each attempt bills at its own model's rates.
- Timeouts: `first_token` bounds only the wait for the first event; gaps after that are
  bounded by `stream_idle` (thinking models can be silent for minutes). Transport
  errors while reading a stream (which the SDK raises unwrapped) are mapped like any
  other provider error, and a stream that ends without `message_stop` (or `[DONE]` for
  OpenAI-compatible providers) is reported as truncated instead of complete.

## Consequences
- Adding a Claude model = alias + pricing + (if it differs from `defaults`) a
  capability line. `tests/test_config.py` checks capability entries match real chains.
- Capability flags must be checked against the provider's docs when models change.
- A mid-stream SDK error arrives as `APIStatusError` with status 200; the adapter maps
  any status < 400 to "failed mid-stream" (covered by a test).
