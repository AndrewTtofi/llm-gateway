# Production deployment

ADRs 0016 and 0023. Two things ship for production: a **release image** and a **production compose
file** that runs on one host. For Kubernetes or a cloud platform, the
[operations page](Operations-and-Deployment.md) lists what to set up; the compose file shows
every piece working together.

## Release image

Pushing a `v*` tag runs `.github/workflows/release.yml`:
- it builds the gateway for **amd64 and arm64**;
- it pushes the image to `ghcr.io/andrewttofi/llm-gateway` with tags `1.3.0`, `1.3` and the
  commit SHA;
- it attaches an **SBOM** and **build provenance**, and signs an attestation with GitHub's
  OIDC identity.

Verify an image before running it:

```bash
gh attestation verify oci://ghcr.io/andrewttofi/llm-gateway:1.3.0 --owner AndrewTtofi
```

## `docker-compose.prod.yml`

```
internet ──443──► Caddy (TLS) ──► gateway ×2 ──► Redis · Redis (cache) · Postgres
                    │
operators ─ssh─► 127.0.0.1:8081 (admin, metrics, readiness)
```

```bash
cp .env.prod.example .env.prod        # fill in; generate secrets with: openssl rand -hex 32
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
docker compose -f docker-compose.prod.yml --env-file .env.prod --profile monitoring up -d   # + Prometheus, Grafana
```

| Service | What it does |
|---------|--------------|
| `caddy` | **Front door:** automatic TLS for `GATEWAY_DOMAIN`, with HSTS. **Public site:** serves only the API; `/admin`, `/metrics`, `/readyz` and the API docs (`/docs`, `/redoc`, `/openapi.json`) are a 404 there. **Operator listener:** a second listener on `127.0.0.1:8081`, reached over an SSH tunnel (`ssh -L 8081:localhost:8081 host`) or a VPN. **Upstreams:** discovers every gateway replica (per-replica health checks and least-connections balancing), flushes streams immediately, caps request bodies, and allows 30-minute requests. **Slow clients:** 10 s to send headers, 2 min for a body, 2 min idle |
| `migrate` | Runs once before the replicas start: `alembic upgrade head`, then creates the read-only database role for Grafana. Gets only the database URL and that role's password |
| `gateway` ×2 | The release image. Read-only filesystem, all capabilities dropped, `no-new-privileges`. Graceful shutdown waits 30 s for open streams, inside a 45 s stop grace period. API docs off (`DOCS_ENABLED=false`): the schema would list the admin API |
| `redis` | Rate limits, budgets, breakers. Password from a config file (not visible in `ps`), append-only persistence |
| `redis-cache` | The response cache only: capped at `CACHE_MAXMEMORY` (512 MB) with LRU eviction, no persistence. Clients decide how fast a cache grows, so it's kept away from limits and budgets (ADR 0023) |
| `postgres` | Keys, usage log, judge scores |
| `prune-usage` | Deletes `usage_log` rows older than `USAGE_RETENTION_DAYS` (default 90) every day. Gets only the database URL |
| `prometheus` *(monitoring)* | Scrapes every replica's `:9100` by DNS discovery; loads the SLO and alert rules |
| `grafana` *(monitoring)* | On `127.0.0.1:3000` only, sign-up off. It reads Postgres as `grafana_ro`, which can read `usage_log`, `judge_scores` and every `api_keys` column **except `key_hash`** |

**Secrets:** each container gets only the variables it needs. The gateway never sees
Grafana's passwords; `migrate` and `prune-usage` never see provider keys or the admin key.
Use hex secrets, because they go into connection URLs as-is. The read-only role's password
must be ASCII.

**Images:** third-party images are pinned by tag *and* digest (`redis:8.10.2-alpine@sha256:…`),
so a re-pushed tag can't change what runs. Dependabot updates both, a week after a release.

Verified locally by building the image and running the whole stack, with these checks:
- TLS and health checks;
- both replicas receiving traffic;
- operator endpoints unreachable from the public site;
- the read-only role refused `key_hash`;
- retention running.

## Maintenance

```bash
docker compose -f docker-compose.prod.yml exec gateway python -m app.maintenance prune --days 90
docker compose -f docker-compose.prod.yml exec gateway python -m app.maintenance grant-readonly --role grafana_ro
```

- **Retention:** `prune` deletes in batches, with short transactions. Export spend history
  you need for finance before it's pruned.
- **Read-only role:** `grant-readonly` creates or updates the role. The password
  (`READONLY_PASSWORD`) is sent as a SCRAM-SHA-256 verifier computed locally, so it never
  appears in plaintext in Postgres logs.

## Upgrading

1. Read the CHANGELOG for the new version, and check whether there are migrations.
2. Set `GATEWAY_VERSION` in `.env.prod`.
3. Run `docker compose … up -d`. `migrate` runs first, then the replicas are replaced, and
   in-flight streams finish during the grace period.

Config changes don't need a release: edit `config/` and run
`docker compose -f docker-compose.prod.yml --env-file .env.prod restart gateway`. That restarts
the replicas one by one, and each reloads the config. `POST /admin/reload` through the
operator listener reaches only the one replica Caddy picks.
