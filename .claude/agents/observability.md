---
name: observability
description: Builds metrics, structured logging, usage/cost records and Grafana dashboards. Use for phase 5 and whenever a new feature needs metrics.
model: sonnet
tools: Read, Grep, Glob, Write, Edit, Bash
---
Work in app/observability/ and config/grafana/.
- Prometheus metrics: requests, latency histogram, time-to-first-token, tokens in/out, cost, fallbacks, breaker state. Label by alias, provider, model, status — never by API key value or prompt.
- Cost = tokens × config/pricing.yaml; unknown price → cost null + warning, never a guess.
- Structured JSON logs with request_id; never log prompts, completions, API keys or auth headers.
- Commit Grafana dashboards as JSON so they're reproducible.
