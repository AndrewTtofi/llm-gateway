# 0025 — Pre-deploy security: scanning, operator credentials, audit, network segmentation

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 12 (pre-deploy hardening)

## Context
Before the first real deployment, four gaps remained from the audit's informational notes
and the pre-deploy review:
- **Dependency scanning:** it ran only during audits.
- **Image scanning:** nothing ever scanned the container image's own packages (Debian, and
  what the Python base image bundles).
- **One shared admin key:** there was no way to know who changed what, and removing one
  person meant rotating everyone's key. Failed admin logins weren't limited.
- **A flat network:** every container on the production compose network could reach
  Caddy's operator listener (`:8081`, where `/admin` is). The databases could reach the
  internet.

## Decision
**Scanning:**
- **CI:** `pip-audit` checks the locked runtime dependencies on every PR (`--no-deps`: the
  lockfile is complete). Trivy scans the built image on every PR.
- **Release workflow:** Trivy scans the amd64 build **before** anything is pushed, so a
  failing scan publishes nothing.
- **What fails a build:** fixable HIGH or CRITICAL findings (`--ignore-unfixed`).
- **How Trivy runs:** as a container image pinned by digest. The repo allows only
  GitHub-owned actions, and Trivy reads a saved image tarball, so it never gets the
  Docker socket.

The first scan found five HIGH findings:
- **Four in pip's bundled `urllib3`, `msgpack` and `setuptools`.** pip isn't needed at run
  time, so the image now removes it after installing.
- **One in Debian's `libpcre2`.** The build now applies Debian security updates.

After both changes the image scans clean.

**Operator credentials:**
- `ADMIN_KEYS_FILE` lists `name sha256-of-key`, one operator per line. The file holds
  hashes, never keys, and is re-read when it changes.
- `GATEWAY_ADMIN_KEY` still works, as the operator `admin`.
- `make admin-key name=…` makes a key and its line.
- Every hash is compared in constant time.

**Audit log:**
- Every admin change (key create, edit and revoke, config reload) is stored in
  `admin_audit` (migration 0008) and logged as `gateway.audit`, with the operator, action,
  target and change details. It never contains key material.
- `GET /admin/audit` lists recent entries.
- A failed write is logged as an error but doesn't block the change.

**Failed logins:** more than 10 failures in a minute from one source get `429
admin_login_limited` for the rest of the minute, even with a right key.
- **Per replica:** the counter is kept separately on each replica.
- **Per source:** behind Caddy all requests share one source, so in practice the limit
  is per replica.

**Networks (production compose):**

| Network | Members | Route out |
|---------|---------|-----------|
| `edge` | Caddy, gateway | yes |
| `data` | Postgres, both Redis, migrate, prune, backup, gateway, Grafana | none (`internal`) |
| `metrics` | Prometheus, Alertmanager, Grafana, gateway | yes |

Only the gateway shares a network with Caddy.

## Consequences
- **Trivy updates are manual:** it's pinned inside workflow `run:` steps, where Dependabot
  doesn't look. Bump it together with Dependabot's other updates.
- **An operator who mistypes** their key ten times waits a minute.
- **Out of scope:**
  - **SSO** for operators, which needs a web front end the gateway doesn't have.
  - **IP allowlists:** behind an SSH tunnel or VPN, the source address says little.
