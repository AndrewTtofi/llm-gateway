# 0009 — How the gateway is benchmarked

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 6

## Context
"Fast" and "reliable" need numbers. The interesting LLM-specific numbers are
per-chunk: time to first token and the gap between streamed chunks — and the
gateway's own overhead, separated from the provider's latency.

## Decision
- **Own load generator** (`tests/load/loadgen.py`, asyncio + httpx) instead of k6 or
  Locust: k6 can't time individual SSE chunks, and Locust's model is closed-loop. Ours
  does **open-loop** load (fixed arrival rate — a slowing server can't slow the test,
  avoiding coordinated omission) and closed-loop (fixed concurrency), and records TTFT,
  every inter-chunk gap and the `server-timing` admission time.
- **Mock provider** (`tests/load/mockllm.py`, OpenAI-compatible): the same request goes
  directly to it or through the gateway, so the difference *is* the gateway. Its
  "realistic" profile **samples** TTFT and inter-chunk gaps from the empirical
  distributions of 60 real Claude Haiku streams (`real_claude_profile.json`, inverse
  CDFs), so load tests have realistic shapes for $0. It honours `max_tokens`.
- **Paired comparisons:** with `--paired`, request *i* carries `seed=i` and the mock uses
  identical delays for it on every run, so direct-vs-gateway differences are computed
  per request and the mock's own randomness cancels out.
- **Honest load:** open-loop latency is measured from each request's *scheduled* start
  (coordinated omission); closed-loop clients ramp in over one stream length; every
  measured run gets a warm-up; a stream only counts as a success if it ends with
  `[DONE]`; every claim in `docs/RESULTS.md` is computed by `report.py` from the JSON.
- **Bench rig** (`docker-compose.bench.yml`) on top of the dev stack: two gateway
  replicas without `--reload` (migrations run once by a one-shot service), Toxiproxy in
  front of Redis and Postgres for dependency chaos, Prometheus scraping both replicas,
  and the load generator inside the Docker network. Published ports bind to 127.0.0.1;
  bench keys may only use bench aliases and are revoked after each run.
- **Scenarios** (`tests/load/run_bench.py`) save JSON; `tests/load/report.py` renders
  `docs/RESULTS.md`.

## Consequences
- Results come from one laptop (WSL2 + Docker Desktop): load generator, gateways,
  Redis, Postgres and mock share the same CPUs. Absolute capacity numbers are a floor,
  not a production figure; relative numbers (overhead, 1 vs 2 replicas, before/after a
  fault) are the meaningful ones.
- Real-provider latency isn't load-tested (cost, provider rate limits); its shape is
  replayed instead.
