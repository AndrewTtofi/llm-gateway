# 0006 — Gateway API keys

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 4

## Context
Every `/v1/*` call must be tied to a client so limits, budgets and (Phase 5) usage can
be attributed. Keys must be revocable and must never be stored in a usable form.

## Decision
- **Format:** `gw_<43 url-safe chars>` (32 random bytes). The `gw_` prefix makes leaked
  keys easy to spot (and to add to secret scanners).
- **Storage:** Postgres `api_keys` stores **SHA-256 of the key**, plus the first 8
  characters for humans ("which key is this?"). Plain SHA-256 is enough here — slow
  hashes (bcrypt/argon2) exist to protect *guessable* passwords; 256 random bits can't
  be brute-forced, and a fast hash keeps per-request lookup cheap. The plaintext is
  shown once, at creation.
- **Per-key settings:** a tier from `config/limits.yaml` (requests/min, tokens/min,
  monthly budget, allowed models) with optional per-key overrides in the database.
- **Lookup cache:** keys are cached in-process for 30 s; revocation takes effect within
  30 s. Malformed keys are rejected before hashing; hits and misses live in separate
  bounded LRUs, so a flood of random keys can't evict valid ones; concurrent lookups of
  one key share a single query. A flood of *well-formed* random keys still costs one
  query each — rate-limiting unauthenticated traffic belongs at the edge (load balancer
  / WAF).
- **Postgres failure mode:** queries time out after `db_timeout_seconds` (2 s). If the
  store is down, a key seen in the last 10 minutes keeps working (stale-if-error) and
  unknown keys get 503 — auth fails closed, but a short Postgres blip isn't an outage.
- **Admin API:** `POST/GET /admin/keys`, `DELETE /admin/keys/{id}`, guarded by
  `GATEWAY_ADMIN_KEY`.
- **Schema** managed by Alembic; the dev stack runs `alembic upgrade head` on start.

## Consequences
- There's no "edit key" endpoint yet: changing a key's tier or overrides means revoking
  it and issuing a new one, whose month-to-date spend starts at zero.
- `/healthz` stays open; everything under `/v1` needs `Authorization: Bearer gw_…`.
- Revocation isn't instant (≤ 30 s); acceptable for a gateway, revisit with a Redis
  pub/sub invalidation if it ever matters.
