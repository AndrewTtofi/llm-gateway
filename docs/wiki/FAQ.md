# FAQ

**Is it only for OpenAI?**
No. "OpenAI-compatible" is the API format clients use. Requests can go to Claude (the first
choice in most built-in aliases), OpenAI, Ollama or any OpenAI-compatible server. Clients
can also use Anthropic's format (`/v1/messages`).

**Can I use my Claude Pro/Max or ChatGPT/Codex subscription instead of API keys?**
Not for the gateway's routes. Subscriptions aren't API credentials, and reusing their login
tokens breaks every major provider's terms. See [Subscriptions and provider terms](Subscriptions-and-Terms.md).

**Does it work with Claude Code?**
Yes. Set `ANTHROPIC_BASE_URL` to the gateway, and use a gateway key as `ANTHROPIC_AUTH_TOKEN`
and gateway aliases as models. Prompt caching and extended thinking don't pass through yet.
See [Providers and translation](Providers-and-Translation.md#using-it-with-claude-code).

**How much latency does it add?**
About +2.6 ms p50 and +3.8 ms p95 per request, measured. Time to first token, which users
notice, is dominated by the provider. See [Testing and benchmarks](Testing-and-Benchmarks.md).

**What happens if Redis goes down?**
Requests keep flowing. Rate limits, budgets and breakers fail open until Redis returns. See
[Architecture → Degraded modes](Architecture.md#degraded-modes).

**What if Postgres goes down?**
Keys seen in the last 10 minutes keep working from cache. New or unknown keys get 503.
Usage rows are dropped and counted.

**Why did my request get a different model than I expected?**
Check `x-gateway-provider` and `x-gateway-fallback`. The first target failed, or its breaker
was open, so the next one in the chain served. `GET /admin/providers` shows breaker states.

**Why am I rate-limited when I've barely used any tokens?**
The limiter reserves `max_tokens` up front and refunds the unused part when the request
finishes. A client sending `max_tokens: 32000` uses a lot of tokens/min while requests are in
flight. Lower `max_tokens`, or raise the key's `tokens_per_minute`.

**Why can't a stream fall back after it starts?**
The client has already received part of an answer. Switching models would join two answers
together. See [Routing and reliability → The first-token boundary](Routing-and-Reliability.md#the-first-token-boundary).

**How do I add a model?**
Add the target to an alias chain (and, for Anthropic, its capability flags) in
`config/models.yaml`, and add its price to `config/pricing.yaml`. Then run `make reload`.
See [Configuration reference](Configuration-Reference.md) and
[`docs/CHANGING-MODELS.md`](https://github.com/AndrewTtofi/llm-gateway/blob/main/docs/CHANGING-MODELS.md).

**How accurate is the cost tracking?**
It's as accurate as `pricing.yaml` and the providers' reported usage. Cached tokens are
billed at the cached price, and each refusal-fallback attempt at its own model's price. Keep
the prices current, since providers change them.

**Does it support embeddings, images, audio, or OpenAI's Responses API?**
Not yet. It handles chat (`/v1/chat/completions`, `/v1/messages`), including tools, image
*inputs* and streaming.

**Does it store my prompts?**
No. It logs and stores sizes, token counts, costs and latencies, never content.

**Can several teams share one gateway?**
Yes. That's the main use case: one key per app or team, each with its own limits, budget and
allowed models. See [Use case: one gateway for many apps](Use-Case-Multi-App.md).
