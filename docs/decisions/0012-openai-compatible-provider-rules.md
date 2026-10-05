# 0012 — Per-provider rules for OpenAI-compatible APIs, and the `frontier` alias

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 8

## Context
The owner wanted each company's top model available behind the gateway: Claude Fable 5.1,
GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7, Mistral Medium 3.5 and DeepSeek's best. All except
Anthropic are reached through "OpenAI-compatible" chat-completions APIs. The research
against each provider's docs (2026-10-05) showed that "compatible" hides real differences:

- **OpenAI GPT-6 Astra / GPT-6.1 Sol:** no tool calling on Chat Completions (Responses API
  only); sampling parameters must be removed; `reasoning_effort: none` is a 400;
  `max_completion_tokens` replaces `max_tokens`.
- **Mistral:** rejects unknown fields with a 422. `stream_options`, `max_completion_tokens`,
  `seed` and `user` aren't Mistral parameters. With `reasoning_effort: high`, `content`
  becomes a list of chunks instead of a string.
- **xAI Grok 4.7:** reasoning models reject `presence_penalty`, `frequency_penalty` and `stop`.
- **Gemini:** thinking can't be turned off; the compatibility layer is beta, and 3.1 Pro is
  preview only.
- **DeepSeek:** V4-Pro is being phased out in favour of V4.1 Flash (`deepseek-flash`), which
  DeepSeek says is better. Prices differ at peak and off-peak hours.

This matters more than it seems. A provider rejecting a parameter answers 400, which the
router rightly treats as a client fault: it returns the error and does **not** fall back
(ADR 0004). One provider's quirk would therefore break a cross-provider chain for every
client.

## Options considered
1. **Translate per provider in code** (adapters per vendor). Each would be exact, but it
   means a lot of code for small differences, and model behaviour changes faster than code.
2. **Forward everything and let errors fall back.** Simple, but 400s don't fall back, and
   reclassifying them would hide real client errors.
3. **Declarative rules in config** for the OpenAI-compatible adapter: per provider and per
   model, `allow` / `drop` / `rename` / `values`, plus `tools: false` and `vision: false`
   capability gates.

## Decision
Option 3 (`app/providers/openai_compat.py::shape_request`, configured in `models.yaml`):

- **Rules:** model rules overlay provider rules. `drop` is a union; `rename` and `values`
  merge; a model's `allow` replaces the provider's. `model`, `messages`, `stream` and
  `stream_options` are always kept (`stream_usage: false` still removes the last one).
- **Unsupported values:** a value outside `values` is removed, so the provider uses its
  default rather than failing.
- **Capability gates:** a request with tools for a `tools: false` model, or with images for a
  `vision: false` model, raises `UnsupportedRequest`. The router then skips that target, so
  it doesn't count against the breaker, and the chain continues.
- **Missing keys:** a provider whose `api_key_env` isn't set now fails fast and falls back
  without a network call. Previously it sent an unauthenticated request and got a 401.
  `/v1/catalog` shows `configured: false` for its models.
- **Capabilities shown:** `/v1/catalog` lists the capabilities the gateway can use, which
  can be fewer than the model has. Catalog facts confirmed by hand can be `pinned`, so the
  price sync doesn't keep proposing the public catalogs' values.
- **New providers:** `gemini`, `xai`, `mistral` and `deepseek`, each with its documented rules.
  - **Prices:** standard-tier prices, all confirmed by both public catalogs. DeepSeek uses its
    peak price (off-peak is half), which is conservative for budgets.
  - **Long-context tiers:** not modelled. Gemini, xAI and OpenAI charge more above
    200K–272K prompt tokens.
- **The `frontier` alias:** Fable 5.1 → Opus 5.5 → GPT-6 Astra → Gemini 3.1 Pro →
  Grok 4.7 → Mistral Medium 3.5 → DeepSeek V4.1 Flash.
  - Existing aliases are unchanged, so nothing gets more expensive unless an app asks for it.
  - `standard` keys may use it, and their budgets cap the spend.

## Consequences
- One provider's parameter quirks no longer break a mixed chain. Adding the next
  OpenAI-compatible provider is usually YAML only.
- **Dropping parameters silently changes behaviour.** For example, `stop` is dropped for xAI
  rather than honoured. That's the price of availability. Clients that rely on a parameter
  should use an alias whose targets all support it.
- **GPT-6 Astra and Sol can't serve tool-using requests** until the gateway has a
  Responses-API adapter (a follow-up). The existing `smart` and `balanced` chains already
  fall through to the next target for those requests.
- **Not yet tested live:** Gemini, xAI, Mistral and DeepSeek have not been called with real
  keys. The rules come from their documentation. Run `make test-live` once keys exist, and
  expect a rule or two to need adjusting.
- **Facts change:** models change behaviour without notice. The weekly price/catalog check
  catches prices and facts, but not parameter rules. Recheck them when a provider ships a
  new model.
