# 0015 — Long-context tiers, cache-write prices and off-peak pricing

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 9

## Context
`pricing.yaml` held one input, output and cached-input price per model. Real prices have
three more dimensions, which made spend reports and budgets wrong exactly where requests
are expensive:

- **Long-context tiers.** OpenAI above 272K prompt tokens (2× input and cache, 1.5× output),
  and Gemini and xAI above 200K. The *whole* request is repriced.
- **Cache writes.** Anthropic charges 1.25× input for 5-minute writes and 2× for 1-hour
  writes. OpenAI lists cache-write prices too. These were billed at the plain input price.
- **Time of day.** DeepSeek charges half outside peak hours (01:00–04:00 and 06:00–10:00 UTC,
  Monday–Friday).

## Decision
- **`Price`** gains:
  - `cache_write` and `cache_write_1h` (a missing price falls back to input or
    `cache_write`);
  - `tiers: [{above_prompt_tokens, input, output, cached_input, cache_write, cache_write_1h}]`,
    ascending. The last tier the prompt exceeds sets every rate for the request;
  - `off_peak: {multiplier, peak_utc: [HH:MM-HH:MM], peak_days}`. Outside the windows,
    the cost is multiplied.
- **Cost:** `cost()` splits the prompt into plain, cache-read, cache-write and 1-hour-write
  tokens. Cache writes come from Anthropic's `cache_creation_input_tokens` (with the
  `ephemeral_1h` breakdown) and from OpenAI Responses' `cache_write_tokens`, via the usage
  extension fields (ADR 0013).
- **Validation:** the gateway rejects negative or non-finite prices, unordered tiers and
  malformed windows on load.
- **Sync (`make prices`):** proposes cache-write prices (LiteLLM `cache_creation_*`,
  OpenRouter `input_cache_write*`) and tiers (LiteLLM `*_above_<N>k_tokens`, OpenRouter
  `overrides`), cross-checked like base prices.
  - It also only uses a LiteLLM entry under a bare model ID if LiteLLM files it under the
    same provider. A Vertex AI listing was being mistaken for Gemini's.
  - Off-peak windows have no public source and are maintained by hand.

## Consequences
- **Accurate costs:** reports and budgets now match provider invoices for long prompts,
  cached agents and off-peak traffic.
- **Holidays aren't modelled:** DeepSeek's Chinese public holidays are billed at the peak
  price, which over-reports rather than under-reports.
- **Budget reservations** still use the base price at the chain's first target, before the
  prompt size is known. Settlement then uses the right tier. A tier jump can make one
  request overshoot a budget by more than before. The overshoot is bounded by one request.
- **Not modelled:** batch and flex service tiers. The gateway doesn't use them.
