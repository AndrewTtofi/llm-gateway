# Security

Report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/AndrewTtofi/llm-gateway/security/advisories/new)
(see `SECURITY.md`).

## Threat model in brief

The gateway holds the organisation's provider API keys and sees every prompt. The main risks:

| Risk | Mitigation |
|------|------------|
| Leaking provider keys | Env vars only. Never logged, never returned. Provider error text (which can contain key fragments or org IDs) is withheld from clients except for client-caused 400/413/422 (ADR 0002) |
| Leaking gateway keys | Stored as SHA-256 hashes; shown once at creation; never logged. `make key` passes the admin key to curl on stdin, so it doesn't show up in `ps` |
| Leaking prompts | Prompt or completion content is never logged or stored; only sizes, token counts and costs. `user` (and Anthropic's `metadata.user_id`, the Responses API's `safety_identifier`) is sent to providers as a SHA-256 pseudonym. `store`, `background` and `metadata` aren't forwarded unless a provider is configured to pass them |
| One app spending everything | Per-key and per-team budgets and token limits; budget reservations stop concurrent overspend |
| Usage that escapes billing | Reasoning output is counted; a request cut off before the provider reports usage is billed at least for the time it ran; timed-out attempts are billed; parameters that change the price (`service_tier`, search options, audio, predicted outputs) are held back (ADR 0023) |
| One tenant degrading others | A per-key limit on requests in flight; a stalled reader is disconnected after 30 s; gateway-side failures (full pool, a long request past its own time limit) never count against the shared circuit breakers (ADR 0023) |
| Unauthorised model use | Per-tier `allowed_aliases`; direct `provider/model` access only to known targets |
| Resource exhaustion | Format check before any key lookup; the key is checked before the body is parsed; a miss cache for unknown keys; bounded metric labels; at most 10 000 messages per request; connection pools per provider; upstream calls cancelled on disconnect |
| Admin takeover | `/admin/*` disabled unless `GATEWAY_ADMIN_KEY` is set; constant-time comparison; restrict it at the network level too |
| Injected config | Config is local YAML, not user input. Clients can't make the gateway call arbitrary URLs: only configured providers are called |
| Prompt injection | A heuristic filter (plus an optional classifier) over user messages and tool results, logging, flagging or blocking per tier ([Quality and safety](Quality-and-Safety.md)) |
| Huge requests | Bodies over `MAX_BODY_BYTES` get a 413 before parsing; Caddy caps them at the edge too |
| Cross-tenant cache leaks and poisoning | The response cache is scoped per key by default; team or global sharing is an explicit choice. In shared scopes a caller can't force a refresh, and semantic matching needs `shared_semantic: true` ([Response cache](Response-Cache.md#scope-who-shares-answers)) |
| Forged request ids | `x-request-id` is always the gateway's own; a caller's id is kept separately, so it can't collide with other tenants' rows |
| Dashboard credentials | In production, Grafana reads Postgres through a read-only role that can't see `key_hash` |

## Recommendations for operators

- **TLS:** terminate it at the load balancer. Keys travel in headers.
- **Network:** keep `/admin/*`, `:9100` and ideally `/readyz` off the public internet.
- **Keys:** one per app or team, never shared. Revoke on offboarding. Revocation reaches
  every replica at once through Redis, or within 30 s if Redis is down.
- **Admin key:** at least 32 random characters (`openssl rand -hex 32`); the gateway warns at
  startup about a shorter one.
- **Provider keys:** rotate them in your secret store. The gateway only reads env vars at
  startup, so restart replicas after a rotation.
- **Monitoring:** alert on `upstream_quota_exhausted` and on fallback-rate spikes. Fallback
  keeps answers flowing, which can hide a problem with your account.
- **Production:** never set `GATEWAY_ENABLE_FAKE` there.
- **Hardening:** the production compose file shows the hardening in practice:
  - read-only containers with all capabilities dropped;
  - operator endpoints only on a localhost listener;
  - each container gets only its own secrets (`migrate` and `prune-usage` only the database);
  - the Redis password stays out of process arguments;
  - the response cache has its own capped Redis, so it can't crowd out limits and budgets;
  - API docs off, HSTS and slow-client timeouts at Caddy;
  - third-party images pinned by digest.

  See [Production deployment](Production-Deployment.md).
- **Image provenance:** release images carry an SBOM and signed provenance. Verify them with
  `gh attestation verify` before deploying.

## Security audit (October 2026)

A full audit of the gateway found no auth bypass, no SQL, Lua or header injection, no SSRF and
no secret leakage, and `pip-audit` reported no known vulnerable dependency. What it did find
is fixed and covered by `tests/test_hardening.py`. [ADR 0023](../decisions/0023-security-hardening.md)
has the details:

| Finding | Fix |
|---------|-----|
| Reasoning, hang-ups and timeouts went unbilled | Reasoning counted; interrupted requests billed for the time they ran; timed-out attempts billed |
| One key could open a provider's breaker for every tenant | Full pools and request-length timeouts don't count; per-key concurrency limit; stalled readers disconnected |
| The token estimate ignored tools, files and tool calls | All counted; the provider's real default `max_tokens` used |
| One oversized value could drop usage rows | Bounded `max_tokens`, 64-bit columns, a rejected row costs only itself |
| Pricing parameters passed through | Held back unless a provider passes them |
| Huge bodies parsed before the key check | Key first; message and part counts bounded |
| Injection hidden at the start of a long message, or in tool descriptions | Both ends scanned, tools scanned, unscanned text reported |
| Shared caches could be poisoned | No refresh in shared scopes; shared semantic matching is opt-in |
| Spend lost during a Redis outage | Queued and written later; budgets use the last known spend |
| Smaller ones | Metric labels, `n` validation, deep JSON, `user` hashing, request ids, revocation broadcast, ASCII-only role passwords, helper-container secrets, Caddy, docs, image digests, Dependabot cooldown |

## Repository hygiene

The repo is public:

- branch protection on `main`, with changes only by pull request and CI required;
- Dependabot (with a 7-day cooldown on new releases), and pinned, hashed dependencies
  (`requirements*.txt`);
- a `.dockerignore`, so `.env` files never enter the image build context;
- no secrets in CI;
- the dev stack binds ports to `127.0.0.1` only.
