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
| Leaking prompts | Prompt or completion content is never logged or stored; only sizes, token counts and costs. `user` / `metadata.user_id` are hashed before reaching providers |
| One app spending everything | Per-key budgets and token limits; budget reservations stop concurrent overspend |
| Unauthorised model use | Per-tier `allowed_aliases`; direct `provider/model` access only to known targets |
| Resource exhaustion | Format check before any key lookup; a miss cache for unknown keys; bounded metric labels; connection pools per provider; upstream calls cancelled on disconnect |
| Admin takeover | `/admin/*` disabled unless `GATEWAY_ADMIN_KEY` is set; constant-time comparison; restrict it at the network level too |
| Injected config | Config is local YAML, not user input. Clients can't make the gateway call arbitrary URLs: only configured providers are called |
| Prompt injection | A heuristic filter (plus an optional classifier) over user messages and tool results, logging, flagging or blocking per tier ([Quality and safety](Quality-and-Safety.md)) |
| Huge requests | Bodies over `MAX_BODY_BYTES` get a 413 before parsing; Caddy caps them at the edge too |
| Cross-tenant cache leaks | The response cache is scoped per key by default; team or global sharing is an explicit choice ([Response cache](Response-Cache.md#scope-who-shares-answers)) |
| Dashboard credentials | In production, Grafana reads Postgres through a read-only role that can't see `key_hash` |

## Recommendations for operators

- **TLS:** terminate it at the load balancer. Keys travel in headers.
- **Network:** keep `/admin/*`, `:9100` and ideally `/readyz` off the public internet.
- **Keys:** one per app or team, never shared. Revoke on offboarding; revocation takes effect
  within 30 s on all replicas.
- **Provider keys:** rotate them in your secret store. The gateway only reads env vars at
  startup, so restart replicas after a rotation.
- **Monitoring:** alert on `upstream_quota_exhausted` and on fallback-rate spikes. Fallback
  keeps answers flowing, which can hide a problem with your account.
- **Production:** never set `GATEWAY_ENABLE_FAKE` there.
- **Hardening:** the production compose file shows the hardening in practice:
  - read-only containers with all capabilities dropped;
  - operator endpoints only on a localhost listener;
  - each container gets only its own secrets;
  - the Redis password stays out of process arguments.

  See [Production deployment](Production-Deployment.md).
- **Image provenance:** release images carry an SBOM and signed provenance. Verify them with
  `gh attestation verify` before deploying.

## Repository hygiene

The repo is public:

- branch protection on `main`, with changes only by pull request and CI required;
- Dependabot, and pinned, hashed dependencies (`requirements*.txt`);
- no secrets in CI;
- the dev stack binds ports to `127.0.0.1` only.
