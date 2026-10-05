# 0014 — OpenAI Responses API adapter

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 9

## Context
GPT-6 Astra and GPT-6.1 Sol are OpenAI's top models, but "tool calling requires Responses":
on Chat Completions they can't use tools. ADR 0012 worked around it by skipping them for
tool requests. That throws away the best OpenAI models for agents. Their `pro` reasoning
mode also exists only in the Responses API.

## Options considered
1. **A new provider type** (`openai_responses`). It would duplicate the transport, auth,
   timeouts, pools and errors of the OpenAI-compatible adapter.
2. **A per-model switch in the OpenAI-compatible adapter:** `api: responses`. It reuses
   everything except the request/response translation.
3. **Keep skipping them.**

## Decision
Option 2. `app/providers/openai_responses.py` translates (field names from the official
SDK's types):

- **Request:**
  - messages → `input` items (`input_text` / `input_image` parts);
  - assistant tool calls → `function_call` items, tool results → `function_call_output`;
  - flat `tools` (`strict` defaults to false, matching chat completions), `tool_choice`;
  - `max_output_tokens`;
  - `reasoning: {effort, mode}`, where `mode` comes from the model's config (`reasoning_mode`);
  - `text.format` for structured output;
  - `safety_identifier` from `user`.
- **Not sent:** `stop` and `n` have no equivalent (`n > 1` is a skip). `store: false`
  always: otherwise OpenAI keeps every prompt and answer.
- **Response:** output items → message text, refusal and tool calls. Finish reason from
  `status` / `incomplete_details`. Usage includes cached and cache-write input tokens and
  reasoning tokens.
- **Stream:** named events (`response.output_text.delta`, `response.output_item.added`,
  `response.function_call_arguments.delta`, `response.completed`, …) → chunks.
  - A stream that ends without a terminal event is "ended early" (in-band error, ADR 0005).
  - `response.failed` and `error` are provider errors.

The parameter rules (ADR 0012) apply before the translation, so drops, renames and allowed
values still hold.

## Consequences
- **Tools are back:** Astra and Sol serve tool requests again, in `smart`, `balanced` and
  `frontier`.
- **Pro mode:** add a model entry with `reasoning_mode: pro`. None is configured, because
  pro-mode pricing isn't published separately.
- **Not tested live yet:** the Responses path is tested against mocks built from the SDK's
  types. It hasn't been run against OpenAI, because there's no key yet. `make test-live`
  covers it once there is.
- **No stored responses:** server-side conversation state (`previous_response_id`) isn't
  used. The gateway stays stateless, and clients send the whole conversation, as with
  chat completions.
