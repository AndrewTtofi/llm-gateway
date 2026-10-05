# Providers and translation

The internal format is **OpenAI chat completions**. Every adapter implements
`app/providers/base.py::ProviderAdapter` and translates from that format to the provider's
API and back, including streams, usage and errors.

| `type` in `models.yaml` | Adapter | Used for |
|-------------------------|---------|----------|
| `openai` | `openai_compat.py` (httpx) | OpenAI, Google Gemini, xAI, Mistral, DeepSeek, Ollama, vLLM, LM Studio, OpenRouter, any OpenAI-compatible server |
| `anthropic` | `anthropic.py` + `anthropic_format.py` (official `anthropic` SDK) | Claude |
| `fake` | `fake.py` | Chaos testing and demos; loaded only with `GATEWAY_ENABLE_FAKE=1` |

## OpenAI-compatible providers

The request is forwarded almost unchanged; only the alias is swapped for the upstream model
ID. Unknown fields pass through, so new OpenAI parameters work without a gateway change.

- **Streaming usage:** the gateway always sets `stream_options.include_usage` so it can meter
  streams, and strips the usage chunk again if the client didn't ask for it. For servers that
  reject `stream_options`, set `stream_usage: false` on the provider. The gateway then
  estimates usage from the relayed characters.
- **No API key** (Ollama, local vLLM): `api_key_env: null`.
- **Errors:** upstream errors are classified (see [Routing and reliability](Routing-and-Reliability.md)).
  The provider's raw message is shown to the client only for client-caused 400/413/422,
  because other errors can contain key fragments, org IDs or internal hostnames (ADR 0002).

- **Missing key:** a provider whose `api_key_env` isn't set is skipped, like an open breaker:
  no network call and no breaker effect. `/v1/catalog` shows its models as
  `configured: false`. Keys are read when the gateway starts, so restart it after adding one.

### Parameter rules

"OpenAI-compatible" APIs disagree about parameters. Some reject fields they don't know (422),
some reject specific ones (400), and some models can't use tools through chat completions.
A 400 is treated as the client's fault, so the router won't fall back on it. Each provider
or model therefore declares what it accepts (ADR 0012):

```yaml
providers:
  mistral:
    stream_usage: false          # don't send stream_options
    params:
      allow: [max_tokens, temperature, top_p, stop, tools, tool_choice, …]   # strict API
      rename: { max_completion_tokens: max_tokens, seed: random_seed }
  openai:
    params:
      rename: { max_tokens: max_completion_tokens }
    models:
      gpt-6-astra:
        tools: false             # tool calling needs the Responses API
        params:
          drop: [temperature, top_p, logprobs, top_logprobs]
          values: { reasoning_effort: [low, medium, high, xhigh, max] }
```

| Rule | Effect |
|------|--------|
| `allow` | Only these fields are sent (plus `model`, `messages`, `stream`, `stream_options`) |
| `drop` | Never sent |
| `rename` | Sent under another name; if the client sent both, the new name wins |
| `values` | Sent only with one of these values; otherwise removed, so the provider uses its default |
| `pass` | Let through fields that are held back by default (below) |
| `tools: false` / `vision: false` | A request with tools or images skips this target and the chain continues. `/v1/catalog` doesn't list the capability |

Model rules overlay provider rules. They apply in the order rename → drop → allow →
values, so `drop`, `allow` and `values` use the names fields are *sent* under.
`stream_options` is only ever sent on streams, and not to providers with
`stream_usage: false`.

Dropped parameters change behaviour silently: xAI's reasoning models don't honour `stop`,
for example. That's the trade for a chain that keeps working.

**Held back by default (ADR 0023).** Some fields are never forwarded unless the provider lists
them under `params.pass`:

| Field | Held back because |
|-------|-------------------|
| `service_tier` | Priority tiers cost about twice the standard price, which the gateway's pricing doesn't know |
| `web_search_options`, `search_parameters` | Per-call search fees (OpenAI, xAI) on top of tokens |
| `audio`, `modalities` | Audio output is priced per audio token |
| `prediction` | Predicted outputs bill rejected prediction tokens as output |
| `store`, `background`, `metadata` | They keep tenants' prompts and answers stored in the operator's provider account |

`user` is always sent as a SHA-256 pseudonym, never as given (it's often an email address).
The Responses API's `safety_identifier` gets the same pseudonym.

`make test-live` runs a chat, a stream and a tool call against each provider whose key is
set. Run it after adding a key, or when a provider ships a new model.

| Provider | What its rules handle |
|----------|-----------------------|
| `openai` | `max_completion_tokens`. GPT-6 Astra and Sol: no tools on chat completions, no sampling parameters, `reasoning_effort` ≠ none |
| `gemini` | OpenAI-compatibility endpoint (beta); thinking can't be disabled, so `reasoning_effort: none` is removed |
| `xai` | `presence_penalty`, `frequency_penalty` and `stop` dropped (reasoning models reject them) |
| `mistral` | An allowlist (unknown fields are a 422); `stream_options` not sent; `reasoning_effort` not sent (it would make `content` a list) |
| `deepseek` | `max_tokens`; `reasoning_effort` values none/low/high/max |

These rules come from each provider's documentation (checked 2026-10-05). Gemini, xAI,
Mistral and DeepSeek have not been run live yet.

To add a server, add it in YAML; no code is needed:

```yaml
providers:
  vllm:
    type: openai
    base_url: http://vllm.internal:8000/v1
    api_key_env: VLLM_API_KEY        # or null
    timeouts: { connect: 2, first_token: 60, stream_idle: 120, stream_total: 900, total: 300, pool: 5 }
    limits: { max_connections: 50, max_keepalive: 10 }
```

### OpenAI's Responses API

GPT-6 Astra and GPT-6.1 Sol can only call tools through OpenAI's newer **Responses API**
(`/v1/responses`), not chat completions. A model with `api: responses` is translated
(ADR 0014):

| Chat completions | Responses API |
|------------------|---------------|
| messages | `input` items; images become `input_image` |
| assistant `tool_calls` | `function_call` items |
| `tool` messages | `function_call_output` items |
| `tools` | flat `{type: function, name, parameters, strict}` |
| `max_tokens` / `max_completion_tokens` | `max_output_tokens` |
| `reasoning_effort` | `reasoning.effort` (plus `reasoning.mode` from the model's `reasoning_mode`, e.g. `pro`) |
| `response_format` | `text.format` |
| `user` | `safety_identifier` |

- **Not stored:** `store: false` is always sent, so OpenAI doesn't keep the conversation.
- **Responses and streams:** output items become message text, refusal and tool calls.
  Streamed events (`response.output_text.delta`, `response.function_call_arguments.delta`,
  `response.completed`, …) become chunks. A stream that ends without a terminal event is an
  in-band error.
- **Usage:** includes cache reads, cache writes and reasoning tokens.
- **Limits:**
  - `stop`, `seed` and `logprobs` have no equivalent and are dropped.
  - Reasoning isn't carried between turns yet (ADR 0014), so expect somewhat more reasoning
    tokens in agent loops.

## Anthropic (Claude), outbound

The adapter uses the official `anthropic` SDK for its typed errors, SSE parsing and retry
classification. It translates OpenAI → Messages API (ADR 0003).

### Requests

- **System prompts:**
  - `system` and `developer` messages are collected into the top-level `system` field.
    Anthropic has no system role inside `messages`.
  - A system message in the middle of a conversation moves to the front.
  - The conversation must start with a user turn, so a placeholder is inserted if needed.
- **Consecutive same-role turns** are merged. Parallel tool results must arrive as one user
  message with several `tool_result` blocks.
- **Content:**
  - Images (`image_url`) become base64 or URL image blocks; jpeg, png, gif and webp only.
  - Other content part types → 400.
- **Tools:**
  - Function tools become `{name, description, input_schema}`.
  - `strict: true` schemas are rewritten by the SDK to fit Anthropic's constraints.
  - When streaming, tool arguments stream as they're generated (`eager_input_streaming`).
- **`tool_choice`:** `auto`, `none`, `required` → `any`, and a named function → `tool`.
  `parallel_tool_calls: false` → `disable_parallel_tool_use`.
- **Sampling parameters:**
  - `temperature` is clamped to 0–1 (OpenAI allows 0–2).
  - `temperature` and `top_p` together → `temperature` wins, because newer Claude models
    reject both.
- **`max_tokens`** is required by Anthropic. If the client omits it, the provider's
  `default_max_tokens` applies.
- **Other parameters:**
  - `stop` → `stop_sequences`.
  - `user` → SHA-256 → `metadata.user_id`. Clients often put an email address in `user`.
  - `response_format: json_schema` → `output_config.format`. `json_object` becomes a system
    instruction.
  - `n > 1` → 400. That fault is specific to this provider, so another target in the chain
    may still serve the request.

### Capability flags

Models differ in what they accept. Instead of `if model == …` in code, each model has flags
in `config/models.yaml`; models not listed get the provider's `defaults`:

| Flag | Effect when false/true |
|------|------------------------|
| `sampling` | false: `temperature`/`top_p` are dropped (newer models return 400 on them) |
| `forced_tool_choice` | false: `required`/named tool choice becomes `auto` plus a system instruction to call the tool |
| `effort` | true: OpenAI `reasoning_effort` → `output_config.effort` |
| `refusal_fallback` | true: enables Anthropic's server-side refusal fallback (beta) |

### Responses and streams

| Anthropic | OpenAI |
|-----------|--------|
| `text` blocks | `message.content` |
| `tool_use` blocks | `tool_calls`, renumbered 0, 1, 2… |
| `stop_reason`: `end_turn` / `stop_sequence` / `pause_turn` | `finish_reason: "stop"` |
| `max_tokens`, `model_context_window_exceeded` | `"length"` |
| `tool_use` | `"tool_calls"` |
| `refusal` | `"content_filter"` |
| `input_tokens` + cache reads + cache writes | `prompt_tokens` (OpenAI's includes cached tokens) |
| `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` |

A few details:

- **Thinking blocks** have no OpenAI equivalent and are dropped.
- **No-argument tool calls:** a tool call with no arguments gets `"{}"`, because clients
  `json.loads()` the arguments and `""` would fail.
- **Refusal fallback:** with `refusal_fallback`, a declining model is replaced mid-stream by
  a fallback model. Tool calls are held back until the message completes, so the declined
  model's calls can be dropped. `usage.iterations` bills every attempt at its own model's
  price.

## Inbound: Anthropic Messages API (`/v1/messages`)

Clients that speak Anthropic's format (the Anthropic SDKs, Claude Code) are translated *into*
the internal format at the edge (`app/messages_api.py`, ADR 0010). They then go through the
same pipeline as any other request, so fallback to non-Claude models, limits, budgets and
metering all apply.

### Translated

- **Messages:**
  - The system prompt (a string or text blocks) becomes a system message.
  - System messages inside `messages` stay system messages (Claude Code sends these).
  - Text and images.
  - **Prompt caching:** `cache_control` on the system prompt, content blocks, tools, tool
    calls and tool results is kept (ADR 0013).
  - **Extended thinking:** the `thinking` parameter and signed `thinking` /
    `redacted_thinking` blocks in the history are kept (ADR 0013).
- **Tool calls:**
  - `tool_use` becomes `tool_calls`.
  - `tool_result` becomes `tool` messages, placed straight after the calls. Images inside a
    tool result move to a following user turn, because OpenAI tool messages are text-only.
  - `is_error` becomes a `[tool error]` prefix.
- **Tool definitions:** custom tools (`name`, `description`, `input_schema`, `strict`).
- **`tool_choice`:** `auto` / `any` / `tool` / `none`, plus `disable_parallel_tool_use`.
- **Other parameters:**
  - `stop_sequences`, `temperature`, `top_p`.
  - `metadata.user_id`, hashed.
  - `output_config.effort` → `reasoning_effort`.

### Responses, streams and errors

- **Responses:**
  - text, `tool_use` (arguments parsed into `input`), refusals and stop reasons;
  - **thinking blocks** with their signatures;
  - usage, including cache reads and cache writes.
- **Streams:**
  - `MessagesStream` turns flat OpenAI deltas into numbered content blocks.
  - Tool-call blocks stay open until all calls finish. Agents run the tool on
    `content_block_stop`, and some providers interleave the arguments of parallel calls.
  - Thinking streams as `thinking_delta` and `signature_delta`, so the SDK reassembles signed
    blocks.
  - `message_start` carries an input-token estimate; `message_delta` carries the real usage.
  - A mid-stream failure is `event: error` with no `message_stop`.
- **Errors:** errors use Anthropic's shape, `{"type":"error","error":{"type","message"}}`,
  with types by status:

  | Status | Type |
  |--------|------|
  | 400 | `invalid_request_error` |
  | 401 | `authentication_error` |
  | 403 | `permission_error` |
  | 404 | `not_found_error` |
  | 429 | `rate_limit_error`, or `billing_error` when the budget is exhausted |
  | 503 / 529 | `overloaded_error` |
  | 504 | `timeout_error` |

### How caching and thinking travel

The internal format is OpenAI's, so Anthropic-only features travel as **extension fields**
(`app/extensions.py`): `cache_control`, `thinking`, `thinking_blocks`, a `thinking` stream
delta, and cache-write counts in usage.

- **Anthropic adapter:** reads them.
- **Other providers:** the fields are stripped. A system prompt split into parts for a
  cache breakpoint becomes a plain string again.
- **OpenAI-format clients:** they never receive these fields, and their own `thinking`
  requests are ignored. They couldn't send the signed blocks back, which Anthropic requires
  on the next tool-use turn.

### Not supported

- **Rejected with a 400:** document blocks, and server tools such as web search or code
  execution. Those run inside Anthropic's API and can't be routed elsewhere.
- **Dropped:** `top_k`.
- **Stop sequences:** which sequence matched is not reported (`stop_sequence` is always null).

### Using it with Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_AUTH_TOKEN=gw_...        # gateway key (API billing, not a subscription)
export ANTHROPIC_MODEL=smart              # gateway aliases
export ANTHROPIC_DEFAULT_HAIKU_MODEL=fast
claude
```

Claude Code requests a large `max_tokens` (often 32 000) every time. The rate limiter
reserves prompt plus `max_tokens` up front, so give the key a generous `tokens_per_minute`.
Prompt caching works through the gateway, so long sessions pay the cache-read price for
the repeated part of the prompt. Cache writes are billed at their own price (ADR 0015).
Why subscriptions can't be used here is covered in [Subscriptions and provider terms](Subscriptions-and-Terms.md).

## Adding a new provider type

For an API that isn't OpenAI-compatible (Gemini's native API or Bedrock, for example):

1. Implement `ProviderAdapter` in `app/providers/<name>.py`: `complete()` and `stream()`
   in OpenAI format, raising `ProviderError` with a status and `retryable`/`timeout` set
   correctly.
2. Register the `type` in `app/providers/__init__.py`.
3. Mock it in tests (respx for httpx, a `MockTransport` for httpx2 SDKs). Never call a paid
   API in unit tests.
4. Write an ADR if the translation involves choices (ADR 0003 is the model to follow).
