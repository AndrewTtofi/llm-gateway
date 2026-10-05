# Core concepts

These are the ideas the rest of the wiki builds on. If you come from platform or DevOps
work, most of the gateway will feel familiar: a reverse proxy with auth, rate limits,
retries and metrics. The new parts are how LLM APIs behave, covered below.

## "OpenAI-compatible" is an API format, not a vendor

OpenAI's chat-completions request shape became the de facto standard for LLM APIs. Most
SDKs, frameworks (LangChain, LlamaIndex, the Vercel AI SDK) and model servers (vLLM, Ollama,
LM Studio) speak it. "OpenAI-compatible" means the gateway accepts that shape. It says
nothing about where the request goes.

The gateway also accepts **Anthropic's Messages format** (`/v1/messages`), for the
Anthropic SDKs and Claude Code.

Inside the gateway, every request is one format: OpenAI chat completions. Inbound
Anthropic requests are converted at the edge, and each provider adapter translates out and
back. Because of this one internal format, a request that started on Claude can fall back
to an OpenAI or Ollama model.

## Providers, models, targets, aliases

| Term | Meaning | Example |
|------|---------|---------|
| **Provider** | An upstream API plus its credentials and timeouts | `anthropic`, `openai`, `ollama` |
| **Model** | A model ID at that provider | `claude-sonnet-5-5` |
| **Target** | `provider/model`, the unit that gets called, measured and circuit-broken | `anthropic/claude-sonnet-5-5` |
| **Alias** | A name clients use, mapped to an ordered **chain** of targets | `smart` → Opus → Sonnet → OpenAI |

Clients send an alias as `model`. When you swap a model, you edit `config/models.yaml` and
run `make reload`; apps don't change. Aliases are the gateway's main abstraction:

- They decouple apps from vendors.
- They make fallback possible, because the chain lists the next choices.
- They give metrics a small, fixed set of labels.

## Tokens

LLMs read and write **tokens**, chunks of text averaging about 4 characters of English.
Providers bill and rate-limit by tokens:

- **Input (prompt) tokens:** everything you send, including the system prompt, the
  conversation history, tool definitions and images.
- **Output (completion) tokens:** what the model writes. Usually priced 4–5× higher than
  input.
- **Cached input tokens:** a prompt prefix the provider has seen recently, billed at a
  fraction of the input price (prompt caching).

The token count isn't known until the provider answers. The gateway **estimates** before
the call (characters ÷ 4 + the requested `max_tokens`) and **reconciles** with the
provider's real usage afterwards. See [Keys, limits and budgets](Keys-Limits-and-Budgets.md).

## Streaming (SSE)

Generating a long answer can take tens of seconds, so LLM APIs **stream** it as
**Server-Sent Events**: a long-lived HTTP response that sends small `data: {...}` events as
tokens are produced.

- **OpenAI style:** flat `chat.completion.chunk` deltas, ending with `data: [DONE]`.
- **Anthropic style:** named events (`message_start`, `content_block_start`,
  `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`) that open,
  fill and close numbered content blocks.

Streaming changes error handling, and this is the most important idea in the codebase:

> Once the first token has been sent to the client, the gateway has already returned
> `200 OK` and part of an answer. It can no longer fall back to another model without
> joining two different answers together.

So the gateway retries and falls back freely **before** the first token. **After** it, a
failure is reported in the stream itself and the stream ends without its end marker. See
[Routing and reliability](Routing-and-Reliability.md#the-first-token-boundary).

## Latency: total time vs time to first token

- **TTFT (time to first token):** how long until the user sees text start to appear. It's
  dominated by the provider processing the prompt, and it's what users perceive as speed.
- **Total latency:** until the last token. It grows with answer length, so it's a weak
  health signal on its own.
- **Inter-chunk gap:** the pause between streamed chunks. A long gap means the stream has
  stalled.

The gateway measures TTFT twice:

- **per target:** the attempt that served, for debugging providers;
- **end to end:** from the moment the request arrived, including failed attempts and
  fallback. This is what the client experienced, and the TTFT SLO uses it.

## Tool calls

Models can ask the caller to run a function ("tool"). The request lists tools with JSON
Schema parameters. The model answers with a tool call (name and arguments), the client runs
it, and sends the result back in the next request. OpenAI and Anthropic represent this
differently:

| | OpenAI | Anthropic |
|---|---|---|
| Tool calls | a `tool_calls` field on the assistant message | `tool_use` content blocks |
| Tool results | separate `tool` messages | `tool_result` blocks inside a user message |

The adapters translate between the two. Agents such as Claude Code are mostly loops of tool
calls.

## Failure classes

Every upstream failure is classified before the gateway decides what to do:

- **Client fault** (400, 413, 422, invalid request): returned to the client as-is. Another
  provider would reject it too.
- **Gateway fault** (bad provider key, wrong model ID, quota exhausted): not the client's
  problem. Fall back, and alert the operator.
- **Transient** (429, 5xx, 529 overloaded, timeouts, connection errors): retry with
  backoff, then fall back.

Details in [Routing and reliability](Routing-and-Reliability.md).
