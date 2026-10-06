# Testing and benchmarks

## Test suites

| Command | What runs | Cost |
|---------|-----------|------|
| `make test` | Unit and integration tests: about 310 tests, with providers mocked | Free; this is what CI runs |
| `make test-e2e` | Against the running stack (`make up`) and Ollama: limits, chaos, local model | Free |
| `make test-live` | Everything, including `@pytest.mark.live` tests against real providers | Costs money |
| `make lint` | ruff + mypy (strict) | — |

How providers are mocked:

- **OpenAI-compatible upstreams** are mocked with **respx**, which intercepts httpx.
- **The Anthropic SDK and OpenAI SDK** are built on httpx2, which respx can't intercept.
  Tests give them an `httpx2.MockTransport` instead (a fake in-memory Anthropic API).
- **The real SDKs are tested against the gateway.** They run as *clients*, in-process via
  `httpx2.ASGITransport`, to prove they work unchanged: `test_openai_sdk.py` and
  `test_messages_api.py`.

Rules:

- New behaviour needs a test.
- Never call a paid API in unit tests.
- Live tests are marked and skipped by default.

## Benchmarks

ADR 0009; full results in [`docs/RESULTS.md`](https://github.com/AndrewTtofi/llm-gateway/blob/main/docs/RESULTS.md).

### The rig

`docker-compose.bench.yml` adds:

- a **mock LLM** (`tests/load/mockllm.py`) that replays real Claude Haiku timing, sampled from
  measured time-to-first-token and inter-chunk-gap distributions, and honours `max_tokens`;
- a **second gateway replica**, run like production: no reload, migrations as a one-shot job;
- **Toxiproxy** in front of Redis and Postgres, to inject latency and outages;
- a **load generator** (`tests/load/loadgen.py`) inside the Docker network, timing every chunk.

### Method

Each choice avoids a common benchmarking mistake:

- **Paired runs.** Request *i* carries `seed=i`, so the mock uses identical delays for request
  *i* on the direct run and the gateway run. Subtracting the two removes the mock's own
  randomness, so a few milliseconds of overhead can be measured.
- **Open-loop load, timed from the scheduled start.** If the load generator falls behind,
  that delay counts against the result. This avoids *coordinated omission*: a closed-loop
  tester that waits for slow responses sends fewer requests exactly when the system is
  slow, and hides the slowness.
- **Measure the rig first.** Direct-to-mock baselines showed the load generator saturating
  before the gateway did. Without them, the gateway would have been blamed.
- **Count every response.** Latency is reported over all responses, not only successes, and
  fault injection is aligned by wall clock.

### Results (laptop)

| | |
|---|---|
| Gateway overhead | **+2.8 ms p50 / +4.0 ms p95** per request; **+3.9 ms (0.7%)** on a realistic TTFT |
| 100k-character prompt | **+6.8 ms p50 / +8.2 ms p95** |
| Concurrent streams, one replica (one core) | Within 10% of a direct connection up to **100**; +54 ms p95 at **200** as the core saturates |
| Throughput, 1 → 2 replicas | **417 → 551 req/s** (rig ceiling 899) |
| Rate-limit accuracy, 2 replicas | **−0.12%** |
| Budget overshoot, 50 concurrent streams | **+0.9%** (atomic budget holds, ADR 0023) |
| Provider outage | **3** requests to the dead provider before the breaker opened; **0** errors reached clients |
| Provider down/slow, Redis slow/down, Postgres down | **100%** served (rate limits off during a Redis outage; uncached keys get 503 during a Postgres outage) |
| SIGTERM with open streams | **97/97** completed |

Re-measured on 2026-10-06 after profiling the request path:
- **Redis:** 4 round trips per request. A success makes no breaker call while nothing is
  failing, and the budget check is the hold itself.
- **Streams:** slow clients are detected by one watchdog per stream, not a timer per chunk.

Compared with v1.3.1: −9% CPU per request, −12% per stream, −0.8 ms p50.

### Reproduce

```bash
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build
python tests/load/run_bench.py          # scenarios: overhead, capacity, scaling, accuracy, breaker, chaos, lifecycle
python tests/load/report.py             # → docs/RESULTS.md
```

Bench keys are restricted to the bench aliases and revoked afterwards, so a benchmark can't
spend real money.
