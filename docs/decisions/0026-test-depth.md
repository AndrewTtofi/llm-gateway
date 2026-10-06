# 0026 — Test depth: fuzzing, golden fixtures, end-to-end production stack in CI

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 12 (pre-deploy hardening)

## Context
The unit and integration tests used hand-written provider mocks: minimal payloads
carrying only the fields the gateway reads. Three kinds of bug can hide behind that:
- **Untrusted input:** input nobody thought of, from clients or providers, crashes a
  translator.
- **Unseen fields:** fields real responses carry but the mocks don't trip something up.
- **Deployment gaps:** the pieces work alone but not together in the production stack (TLS,
  replicas, migrations, backups).

## Options considered
1. **Record real provider responses** and replay them. This is the best fidelity, but it
   costs money, and none could be spent yet.
2. **Fixtures in the providers' documented formats,** with every documented field, plus a
   recorder that replaces them with real responses once spending is allowed.
3. **Property-based fuzzing** of every parser of untrusted input.
4. **The production compose stack in CI** with the fake provider.

## Decision
Options 2, 3 and 4. Option 1 is ready to run as `tools/record_fixtures.py`.

**Fuzzing** (`tests/test_fuzz.py`, Hypothesis; 150 examples per property locally, 400 in
CI):
- **The API never answers 500:** any JSON (or non-JSON) body to `/v1/chat/completions`,
  `/v1/messages` or `count_tokens` gets a 2xx or a 4xx.
- **Provider data fails cleanly:** any sequence of Anthropic or Responses-API stream
  events, and any provider answer, either translates or raises `ProviderError`.
- **Outbound translation:** any request the API accepts either translates for Anthropic
  and the Responses API, or is refused as unsupported.

It found two real bugs, both fixed:
- **A `tool` message without `tool_call_id`** passed validation and crashed the Responses
  translator (a 500). Tool messages now need their call id, `tool_calls` must be
  well-formed, and the Responses translator refuses malformed requests as unsupported, as
  the Anthropic one already did.
- **Malformed provider events or answers** raised `KeyError`/`TypeError`. Before the first
  chunk, that was a 500 instead of a fallback. They now raise a retryable "invalid
  response" `ProviderError`.

**Golden fixtures** (`tests/fixtures/providers/`, `tests/test_golden.py`):
- **Anthropic:** a message, and a stream with thinking, signature and tool-input deltas.
- **OpenAI chat completions:** a tool-call answer, and a stream ending with the usage-only
  chunk.
- **OpenAI Responses API:** a stream with hidden reasoning and a function call.

Each is replayed through the whole gateway, with assertions on what the client receives
and what is billed.

**End to end** (`scripts/e2e_prod.sh`, CI job `e2e`): it builds the image, starts
`docker-compose.prod.yml` with the fake provider, and checks:
- TLS and HSTS;
- two healthy replicas;
- the operator endpoints are hidden from the public site;
- admin key creation, and the audit log;
- chat, streaming and `/v1/messages`;
- a bad key is refused;
- a backup is taken and the restore drill passes;
- usage rows are written.

## Consequences
- **CI is a few minutes longer**, for the e2e job and the stronger fuzzing profile.
- **Fixtures follow the docs, not recordings:** a provider that deviates from its own
  documentation is still only caught live. Run `tools/record_fixtures.py` once keys can be
  used.
- **New parsers need fuzzing too:** a new parser of untrusted input should get a property
  in `test_fuzz.py`.
