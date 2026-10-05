# 0018 — Response cache (exact and semantic)

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
Many workloads repeat themselves: FAQ bots, retries of the same tool call, evaluation
runs. Serving a stored answer is free and instant. A semantic cache goes further by
answering similar questions, at the price of sometimes answering a slightly different one.

## Options considered
1. **Exact only.** Safe and cheap, with a low hit rate on free-text traffic.
2. **Semantic only.** More hits, but needs embeddings for every request, and a wrong hit is
   possible.
3. **Both, opt-in per alias, scoped per caller by default.**

## Decision
Option 3 (`app/cache.py`). An alias may set
`cache: {mode, ttl_seconds, scope, threshold, embedding, max_entries, max_entry_bytes}`.

- **Exact:** SHA-256 of the alias (plus the A/B variant) and the answer-relevant fields:
  messages, tools, sampling, `max_tokens`, stop, seed, effort, thinking. Not `stream`,
  `user` or routing hints.
- **Semantic:**
  - The conversation text is embedded by the configured OpenAI-compatible embedding model.
  - The index is a Redis 8 vector set per scope and alias; a hit is a nearest neighbour
    with similarity ≥ `threshold`.
  - The index stops growing at `max_entries`, and answers still expire.
  - If embedding fails, lookups fall back to exact.
- **Scope:** `key` (default), `team` or `global`. Sharing is a data-isolation decision. A
  semantic hit gives one caller an answer written for someone else's similar prompt.
- **Hits:**
  - No provider call; the token reservation is refunded; cost is 0.
  - They still count as requests against rate limits.
  - They're recorded as target `cache/exact` or `cache/semantic`.
  - Streams are replayed from the stored completion. Anthropic clients get their own
    format, thinking blocks included.
- **Stored:** only clean, complete answers (finish reason `stop` or `tool_calls`), up to
  `max_entry_bytes`.
- **Client control:** `x-gateway-cache: bypass` (no read, no write) or `refresh` (no read,
  write).
- **Failure:** cache errors are logged and ignored. A cache never fails a request.

## Consequences
- **Hits are free and instant** for repeated traffic, and visible in
  `gateway_cache_total`, `x-gateway-cache` and the usage log.
- **Semantic caching trades accuracy for hits.** Use a high threshold and scope it per key
  unless the content is public.
- **Embedding cost:** each semantic lookup costs one embedding call. It isn't metered to
  the caller.
- **Caching changes sampling semantics:** a cached answer is the same every time, even
  with `temperature > 0`. That's why caching is opt-in per alias.
