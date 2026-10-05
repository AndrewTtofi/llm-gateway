---
name: add-provider
description: Add a new LLM provider to the gateway. Usage: /add-provider <name>. Scaffolds the adapter, config entries, pricing and tests.
---
Argument: provider name (e.g. `mistral`, `gemini`, `bedrock`).
1. Check whether the provider exposes an OpenAI-compatible API. If yes, reuse the openai adapter type with a new base_url — no new code.
2. Otherwise use the provider-adapter agent to create app/providers/<name>.py implementing ProviderAdapter, after checking its current API docs.
3. Add the provider block to config/models.yaml (base_url, api_key_env, timeouts) and the key name to .env.example.
4. Add its models to config/pricing.yaml with prices from its pricing page (leave null if unknown, and say so).
5. Use the test-writer agent for respx-mocked tests. Run `make test`.
6. Add a CHANGELOG entry under [Unreleased] → Added.
