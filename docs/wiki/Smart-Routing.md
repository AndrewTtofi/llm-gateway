# Smart routing: policies and A/B tests

An alias normally has a fixed `chain` of models to try in order. Two other kinds of alias
choose the chain **per request**:

| Kind | Chooses by | Use it to |
|------|-----------|-----------|
| `policy` (ADR 0017) | Price, quality, capabilities, context and live latency, from the [catalog](Choosing-Models.md) | Let apps say *what* they need instead of *which* model |
| `variants` (ADR 0020) | Weighted, sticky assignment | Test a model or prompt change on part of the traffic |

## Policy routing (`model: auto`)

```yaml
# config/models.yaml
aliases:
  auto:                                # shipped
    policy: { optimize: cost, min_quality: 3, max_chain: 4 }
  agent:
    policy:
      optimize: quality
      needs: [tools]
      candidates: [anthropic/claude-sonnet-5-5, openai/gpt-6.1-sol, gemini/gemini-3.1-pro-preview]
```

| Field | Meaning |
|-------|---------|
| `optimize` | `cost`: cheapest blended price, then best quality. `quality`: best score, then cheapest. `latency`: fastest measured time to first token on this replica, then cheapest |
| `candidates` | Models to choose from. Empty means every known model, excluding test providers |
| `needs` | Capabilities every chosen model must have: `tools`, `vision`, `reasoning`, `json_schema` |
| `min_quality` | Your 1–5 score from `catalog.yaml`. Unscored models don't qualify |
| `max_blended_price` | USD per 1M tokens, blended 3:1 input to output. Unpriced models don't qualify |
| `max_chain` | How many of the ranked models become the fallback chain (default 4) |
| `client_hints` | Whether clients may send hints at all (default true) |
| `allowed_hints` | Which hints they may send (default all four). `optimize` can move a request to a pricier model (cost → quality); remove it to keep that choice yours |

### What happens per request

1. **Constraints.** Candidates must:
   - have the required capabilities: the policy's `needs`, **plus whatever the request
     implies** (tools → `tools`, image parts → `vision`, a JSON schema → `json_schema`);
   - fit: the context window covers the estimated prompt plus `max_tokens`;
   - meet `min_quality` and `max_blended_price`.

   If none does: **400 `no_route`**. The request asks for something no model offers.
2. **Availability.** Of those, keep the ones whose provider has an API key and whose
   circuit breaker isn't open. If none is left: **503 `no_route`**, retryable.
3. **Ranking.** Rank by `optimize` and take the first `max_chain` as the chain. Retries,
   fallback and breakers then work as for any alias.

The response says what happened: `x-gateway-route: optimize=cost; considered=12; chain=4`,
and `x-gateway-provider` names the model that answered.

### Client hints

Clients can **tighten** a policy, never widen it. They can change `optimize`, add `needs`,
raise `min_quality` or lower `max_blended_price`:

```python
# OpenAI SDK: extra body field
client.chat.completions.create(model="auto", messages=msgs,
                               extra_body={"route": {"optimize": "quality", "needs": ["vision"]}})
```

```bash
curl … -H 'x-gateway-route: optimize=latency; needs=tools; min_quality=4; max_price=5'
```

- **Anthropic clients:** send `route` in the request body.
- **Bad hints:** an unknown hint is a 400 `invalid_route`.
- **Privacy:** hints never reach the provider.

Keys are authorised by alias: a key allowed `auto` may use its candidates.

## A/B tests (variants)

```yaml
aliases:
  support:
    sticky: user          # key (default) | user | request
    variants:
      - { name: control, weight: 90, chain: [anthropic/claude-sonnet-5-5, openai/gpt-6.1-sol] }
      - { name: haiku,   weight: 10, chain: [anthropic/claude-haiku-4-5-20251001],
          system_prefix: "Answer in at most three sentences." }
```

- **Assignment** is a hash of the alias and the caller:
  - `key` (default): the API key.
  - `user`: the request's `user` field, so each end user of an app is split separately.
  - `request`: random every time.

  Sticky assignment keeps a caller on one arm, and the weights hold across callers.
- **Each arm** has its own fallback chain. Its optional `system_prefix` goes in front of the
  system prompt, which is how you test prompt changes.
- **Pinning:** with `allow_pin: true` on the alias, `x-gateway-variant: haiku` forces an
  arm, for QA. It's off by default, so callers can't pick a (pricier) arm or skew the
  experiment.
- **Measured:** the `x-gateway-variant` response header, `usage_log.variant`, the
  `gateway_variant_*` metrics, and the Grafana "A/B" row. That row shows requests, error
  rate and p95 latency per arm, plus a cost-per-request table. Add
  [judge sampling](Quality-and-Safety.md#llm-as-judge-sampling) to compare **quality** too.
- **Cache:** each arm has its own response-cache entries.

The dashboard shows numbers, not significance tests. Run arms long enough to see a stable
difference before deciding.
