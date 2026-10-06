# Write-up: building an LLM gateway as a platform engineer

## What I built

I'm a platform/DevOps engineer, and I used this project to learn AI engineering. The result
is a self-hosted gateway. Apps send OpenAI-format requests to one endpoint, and the gateway
routes each request to Claude, OpenAI or a local Ollama model (Claude and Ollama run live; OpenAI so far only against mocks). It falls back when a provider
fails, limits every API key by requests, tokens and dollars, and records what each request cost.

I built it in seven phases. Each phase had a definition of done, tests, and an ADR for every
decision that wasn't obvious. It ended with load and chaos tests, so the claims below come from
measurements rather than guesses.

## What surprised me

**1. Streaming changes everything about error handling.** In a normal API, a failure means you
retry. With a stream, once the first token has reached the client you have already returned a
200 and sent part of an answer. Falling back at that point would splice two different models'
answers together. So the gateway fails over freely before the first token, and after it reports
the error in-band and stops. That one boundary shaped the router, the timeouts (connect, first
token, idle between chunks, total) and the metrics.

**2. You can't rate-limit tokens you haven't seen yet.** A request's cost is unknown until it
finishes. The limiter estimates tokens up front from the prompt plus `max_tokens`, reserves them,
and reconciles to the real usage afterwards. Budgets work the same way in dollars. With 50
concurrent streams racing for the last dollar of a budget, the overshoot was +3.4%, and with
a realistic store latency a review found a burst could do far worse. Making the check and the
hold one atomic step in Redis brought it to +0.9%.

**3. "OpenAI-compatible" is a contract, not a vendor.** Claude has a different message format,
tool-call shape, streaming events and usage fields. The adapter translates both ways, and model
capabilities are config flags rather than `if model == …` branches. For example, newer Claude
models reject `temperature`. When a model changes, I edit YAML and the code stays the same.

**4. Measuring is harder than building.** My first benchmark numbers were wrong. The mock ignored
`max_tokens`, my load generator saturated before the gateway did, and closed-loop clients moved in
lockstep. The fixes were the following:

- a mock that replays real Claude timing;
- requests paired by seed, so the mock's own noise cancels out;
- open-loop load measured from the scheduled start time, so queueing delay isn't hidden;
- measuring the rig before measuring the system.

**5. The DevOps instincts transfer directly.** Circuit breakers shared across replicas in Redis,
dependencies that fail open, readiness checks that don't take out the whole fleet when Redis
blips, SLO burn-rate alerts, graceful shutdown with open streams: none of that is AI-specific, and
it turned out to be most of the work.

## Numbers (laptop, mock provider with real Claude timing)

- Gateway overhead: **+3.6 ms p50 / +4.9 ms p95**, or **1.0%** of a realistic time to first token (+8 ms with a 100k-character prompt)
- One replica holds **100** concurrent streams within 10% of a direct connection; 1 → 2 replicas: **365 → 478 req/s**
- Provider outage: **3** requests went to the dead provider before the breaker opened, and **0** errors reached clients
- **100%** of requests were served through provider, Redis and Postgres failures. The trade-off: while Redis is down, rate limits are off (budgets keep the last known spend), and while Postgres is down, keys not in the cache get 503. **97/97** streams survived a SIGTERM.

Code, ADRs and full results: https://github.com/AndrewTtofi/llm-gateway

---

## LinkedIn draft (not posted)

> I'm a platform engineer learning AI engineering, so I built the thing platform teams end up
> needing anyway: an LLM gateway.
>
> Apps send OpenAI-format requests to one endpoint. Behind it, requests go to Claude, OpenAI or a
> local model, with automatic fallback, circuit breakers, per-key token and dollar limits, cost
> tracking, and Prometheus/Grafana dashboards.
>
> What I learned:
>
> 🔹 Streaming rewrites error handling. Once the first token has left, you can't fail over without
> splicing two models' answers together. Everything before that moment is retryable; nothing after it is.
>
> 🔹 You can't rate-limit tokens you haven't seen. Estimate, reserve, reconcile. Measured budget
> overshoot under 50 concurrent streams: 0.9%.
>
> 🔹 "OpenAI-compatible" is an API contract, not a vendor lock. Translating to Claude's format is
> where the real work is.
>
> 🔹 My first benchmarks were wrong. Measuring the test rig before the system saved me from
> publishing nonsense.
>
> 🔹 Most of it is classic reliability work: breakers, fail-open dependencies (choosing what to
> give up when Redis dies), readiness probes, SLO burn-rate alerts, graceful shutdown.
>
> Result: +4.9 ms p95 overhead, and 0 client-visible errors during a provider outage.
>
> Code, design decisions and benchmarks: https://github.com/AndrewTtofi/llm-gateway
>
> #PlatformEngineering #AIEngineering #LLM #SRE #DevOps
