# 0016 — Teams, key edits, body limits and production packaging

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 9

## Context
Running the gateway for an organisation needed four things it didn't have:
- spend limits per team, not only per key;
- a way to change a key's limits without re-issuing it to the app;
- protection against huge request bodies;
- a production artefact: a built, provenance-attested image and a production compose file.

## Decision
- **Teams:**
  - Declared in `limits.yaml` (`teams: {name: {monthly_budget_usd}}`), so a typo can't
    create a team.
  - A key has an optional `team`. Admission rejects a request when the team's
    month-to-date spend is at its budget, then checks the key's own budget.
  - Spend is reserved and settled for both, in the same Redis store (`team:<name>`).
  - **Overshoot:** the budget check and the reservation are separate steps (as for keys,
    ADR 0007). Requests that pass the check at the same moment can together overshoot a
    team budget by up to the cost of those requests. Every key in the team adds to that
    concurrency. Closing the gap needs one Lua script that checks and reserves for the key
    and the team atomically. That's a follow-up if teams need hard caps.
  - Moving a key to another team doesn't move this month's spend: it stays with the old
    team.
  - `usage_log.team` records the team at request time, so history doesn't move when a key
    changes team (migration 0004).
  - `GET /admin/teams` lists budget, spend and keys; Grafana has spend-per-team panels.
- **`PATCH /admin/keys/{id}`:**
  - Changes name, tier, team, limits and allowed aliases. `null` clears an override; name
    and tier can't be cleared. The plaintext key is unchanged.
  - Takes effect immediately on the replica that handled the edit, and within the 30 s key
    cache elsewhere.
- **Body limit:**
  - A pure-ASGI middleware returns 413 above `MAX_BODY_BYTES` (default 32 MiB, Anthropic's
    request limit).
  - A declared `Content-Length` is rejected without reading. A chunked body is read up to
    the limit and replayed, and disconnects still reach the app.
  - The middleware runs inside the request-id middleware, so rejections are logged and
    correlated.
- **Release:** a `v*` tag builds amd64 + arm64 images, pushes them to GHCR with an SBOM and
  `mode=max` provenance, and attests them (`actions/attest-build-provenance`). Actions are
  pinned by SHA.
- **`docker-compose.prod.yml`:**
  - **Front door:** Caddy with automatic TLS.
    - The public site serves the API only: `/admin`, `/metrics`, `/readyz` and the API docs
      are a 404 there for everyone. Source IPs can't be trusted for this; behind another
      proxy or Docker's userland proxy, everyone looks private.
    - Operators use a second listener on `127.0.0.1:8081`, reached over an SSH tunnel or a
      VPN.
    - Upstreams are discovered dynamically (`dynamic a`), so balancing and health checks
      are per replica.
    - Request bodies are capped at the edge too. Streams flush immediately, and requests
      may take 30 minutes.
  - **Gateway:** two replicas with read-only root filesystems, all capabilities dropped and
    `no-new-privileges`. The image's graceful-shutdown flag is set, with a longer stop grace
    period.
  - **Data:** a one-shot migration job; Redis with a password (from a config file, not
    the command line) and AOF; Postgres.
  - **Secrets:** each container gets only the variables it needs. Passwords are generated
    as hex, since they go into connection URLs.
  - **Retention:** a daily job (`python -m app.maintenance prune`).
  - **Monitoring (optional profile):** Prometheus discovers every replica by DNS. Grafana is
    bound to localhost, with sign-up disabled and a **read-only database role** that can
    read `usage_log` and every `api_keys` column except `key_hash`. The role's password is
    sent as a SCRAM-SHA-256 verifier computed in Python, so the plaintext never reaches
    Postgres logs.
  - **Image default:** the image's default command still migrates on start, which is
    convenient for one instance. With several replicas, override it as the production
    compose file does.

## Consequences
- **Spend control:** finance gets team-level budgets and reports, and operators adjust
  keys without disrupting apps.
- **A tested production shape:** the whole production stack was brought up locally from a
  locally built image and exercised (TLS, health checks, migrations, roles, retention).
  Cloud-specific packaging (Terraform, Kubernetes manifests) waits for the target choice.
- **Retention deletes history:** `usage_log` is pruned after `USAGE_RETENTION_DAYS`
  (default 90). Export spend for finance before then if it's needed longer.
