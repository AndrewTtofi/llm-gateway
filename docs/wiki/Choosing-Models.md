# Choosing models: catalog and prices

ADR 0011. The gateway can tell your apps what each model costs, what it can do, how good you
consider it, and how it's performing right now. Apps can then pick the cheapest model that
does the job, or the best one when quality matters.

## `GET /v1/catalog`

```bash
curl "localhost:8000/v1/catalog?sort=price&capability=tools" -H "Authorization: Bearer $GW_KEY"
```

```json
{
  "object": "catalog",
  "prices_checked": "2026-10-05",
  "catalog_checked": "2026-10-05",
  "blend": "3:1 input:output tokens",
  "aliases": [{"id": "fast", "chain": ["anthropic/claude-haiku-4-5-20251001", "openai/gpt-6-luna", "ollama/llama3.2:3b"]}],
  "data": [
    {
      "id": "anthropic/claude-sonnet-5-5",
      "provider": "anthropic",
      "callable_directly": true,
      "in_aliases": ["smart", "balanced"],
      "pricing": {"input": 2.0, "output": 10.0, "cached_input": 0.2, "blended": 4.0, "unit": "USD per 1M tokens"},
      "context_window": 1000000,
      "max_output_tokens": 128000,
      "capabilities": ["json_schema", "reasoning", "tools", "vision"],
      "quality": 4,
      "circuit": "closed",
      "live": {"window_seconds": 900, "requests": 412, "attempts": 415, "error_rate": 0.0072,
               "latency_ms": {"p50": 2310.4, "p95": 6120.9}, "ttft_ms": {"p50": 640.2, "p95": 1180.0}}
    }
  ]
}
```

The catalog only shows what **this key** may use. A key on the `dev` tier sees `fast` and
`local` and the models behind them.

| Parameter | Effect |
|-----------|--------|
| `capability=tools` (repeatable) | Only models with every listed capability: `tools`, `vision`, `reasoning`, `json_schema` |
| `min_context=200000` | Only models whose context window is at least this many tokens |
| `sort=` | `price` (blended, cheapest first), `quality` (best first), `ttft` / `latency` (fastest p50 first), `name` (default). Unknown values sort last |

### Reading the fields

- **`pricing`:** USD per 1M tokens. `blended` assumes 3 input tokens per output token, a
  typical mix, so models can be ranked with one number. Your app's mix may differ: summaries
  are input-heavy, generation is output-heavy. `null` means the model isn't priced.
- **`quality`:** *your* score, 1 (basic) to 5 (frontier), set in `config/catalog.yaml`. The
  starting values are a rough guide. Adjust them to your own evaluations, because "best" depends
  on the task.
- **`live`:** what this gateway replica measured over the last 15 minutes, or a shorter
  span under heavy traffic (`window_seconds` says which).
  - `error_rate` counts every upstream attempt, retries included.
  - `latency_ms` and `ttft_ms` cover only requests the model actually served.
  - TTFT is the fairer speed comparison, because total latency depends on answer length.
  - Each replica reports its own traffic. Use Grafana for fleet-wide history.
- **`circuit`:** `open` means the model is being skipped right now. `unknown` means the
  breaker store (Redis) is unreachable.

### Using it from an app

Choose once at startup, or every few minutes, and cache the result:

```python
import httpx

catalog = httpx.get(f"{GATEWAY}/v1/catalog", params={"capability": "tools", "sort": "price"},
                    headers={"Authorization": f"Bearer {KEY}"}).json()["data"]
# cheapest healthy model with quality ≥ 3 and enough context for our documents
pick = next(m for m in catalog
            if (m["quality"] or 0) >= 3 and m["circuit"] != "open"
            and (m["context_window"] or 0) >= 150_000 and m["callable_directly"])
model = pick["id"]          # e.g. "anthropic/claude-haiku-4-5-20251001"
```

**Prefer aliases for production traffic.** A direct `provider/model` call has no fallback
chain. Use the catalog to decide *which alias* an app should use, or to pick a direct model
for offline jobs and experiments. Policy routing (below) will combine both.

### Estimating cost before a call

```
cost ≈ (prompt_tokens − cached) × input + cached × cached_input + max_output × output    (÷ 1 000 000)
```

`POST /v1/messages/count_tokens` gives the gateway's estimate of `prompt_tokens`.

## Keeping prices current

No provider publishes prices through an API, so `make prices` compares your config with two
public catalogs: **LiteLLM's** community price list (primary) and **OpenRouter's** models API
(cross-check).

```bash
make prices                  # show what differs; exits 1 if anything does
make prices ARGS=--write     # apply the changes both sources agree on
git diff config/             # review against the providers' pricing pages
make reload                  # apply without a restart, then commit
```

- **Prices** (`input`, `output`, `cached_input`) go to `config/pricing.yaml`. Only changed
  lines are rewritten, and comments and alignment are kept.
- **Facts** (`context_window`, `max_output_tokens`, `capabilities`) go to `config/catalog.yaml`.
- **Agreement:** a price change is written only when both sources agree. If they disagree
  (`⚠ sources disagree`), or only LiteLLM has the model (`⚠ unchecked`), it isn't written
  unless you add `--force`. Check the provider's own pricing page first.
- **Validation:** remote values are checked before use. Invalid prices or sizes are ignored,
  and the gateway itself refuses negative or non-finite prices.
- **Never changed:** `quality` scores, test providers (`fake`, `dev_only`), and models with no
  public source (local Ollama models). Set those by hand.
- **Different IDs:** if a source names a model differently, add `litellm_id:` or
  `openrouter_id:` to its catalog entry.

**Weekly check.** The `prices` GitHub workflow runs every Monday. If anything drifted, it
opens or updates one issue, *"Model prices or catalog out of date"*, with the diff. It never
edits config itself. Prices bill your teams, so changes go through review (ADR 0011).

**Not modelled:** long-context price tiers (e.g. above 272K input tokens on some OpenAI
models) and batch pricing. The standard rate is used.

## The `frontier` alias

The strongest model from each company, best first:

1. Claude Fable 5.1
2. Claude Opus 5.5
3. GPT-6 Astra
4. Gemini 3.1 Pro
5. Grok 4.7
6. Mistral Medium 3.5
7. DeepSeek V4.1 Flash

Requests with tools skip GPT-6 Astra, which can't call tools through chat completions.
Providers without an API key are skipped too.

Budgets reserve the estimated cost at the chain's *first* target (Fable 5.1, $10 / $50)
and settle at the real price of whichever model served. A `frontier` request therefore
briefly holds more budget than a cheaper fallback ends up spending. Check `GET /v1/catalog?sort=quality` to see
what each one costs and which are `configured`.

## Let the gateway choose: `model: auto`

Instead of picking a model in the app, send `model: "auto"`, plus hints if you like
(`optimize: quality`, `needs: [tools]`). The gateway builds the fallback chain per request
from this catalog: cheapest-first, best-first or fastest-first, skipping open breakers and
models without the needed capabilities or context. See [Smart routing](Smart-Routing.md).
