# Response cache

ADR 0018. The cache is opt-in per alias. A hit returns a stored answer without calling a
provider: no cost, no provider latency.

```yaml
aliases:
  faq:
    chain: [anthropic/claude-haiku-4-5-20251001]
    cache:
      mode: semantic            # exact | semantic
      ttl_seconds: 3600
      scope: key                # key (default) | team | global
      threshold: 0.95           # semantic: similarity needed for a hit (0–1)
      embedding: openai/text-embedding-3-small   # semantic: an OpenAI-compatible embedding model
      max_entries: 10000        # semantic index size per scope and alias
      max_entry_bytes: 262144   # larger answers aren't stored
```

## Exact and semantic

- **Exact.** A SHA-256 of the alias (and A/B arm) plus every field that changes the answer:
  - messages and tools;
  - `tool_choice` and `response_format`;
  - temperature, `top_p` and `max_tokens`;
  - `stop`, `seed`, reasoning effort and thinking.

  `stream`, `user` and routing hints don't change the key, so a streamed and a non-streamed
  request share entries.
- **Semantic.** The conversation text is embedded and looked up in a Redis 8 vector set. A
  neighbour with similarity at or above `threshold` is a hit. An exact match is tried first.
  - **Embedding failure:** if embedding fails (provider down, no key), lookups fall back to
    exact.
  - **Index size:** the index stops growing at `max_entries`, and answers still expire after
    their TTL.

## Scope: who shares answers

| Scope | Shared between | Use when |
|-------|---------------|----------|
| `key` | Requests with the same API key | Default, and always safe |
| `team` | Keys in the same [team](Keys-Limits-and-Budgets.md#teams) | One team's internal tool |
| `global` | Everyone allowed the alias | Public content only: docs Q&A, product FAQ |

A **semantic** hit gives the caller an answer written for *someone else's* similar
question, and that answer may contain details from the other prompt. Only share across
callers when the content isn't private.

## Behaviour

| | |
|---|---|
| Header | `x-gateway-cache: hit`, `miss`, `bypass` or `refresh`. On a hit, `x-gateway-provider: cache/exact` (or `cache/semantic`) |
| Cost | A hit costs 0, refunds its token reservation, and is recorded with target `cache/…` |
| Limits | A hit still counts as one request against requests/min |
| Streams | Streams are replayed from the stored answer, in OpenAI or Anthropic format, thinking blocks included |
| What's stored | Only complete answers: finish reason `stop` or `tool_calls`. Truncated, errored and cut-off answers aren't stored |
| Client control | `x-gateway-cache: bypass` neither reads nor writes. `refresh` skips the read and stores the new answer |
| Failure | Cache errors are logged and ignored. A cache never fails a request |
| Metric | `gateway_cache_total{mode, result}`: `hit_exact`, `hit_semantic`, `miss`, `store`, `bypass`, `refresh` |

## When not to cache

- When answers should vary: creative writing, sampling-based evaluations. A cached answer
  is identical every time, even with `temperature > 0`.
- When requests depend on time or external state the prompt doesn't show.
- With semantic mode, when a near-miss would be harmful (support bots answering a related
  but different question). Keep `threshold` high and test on real traffic.
