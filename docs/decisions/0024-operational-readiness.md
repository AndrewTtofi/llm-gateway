# 0024 — Operational readiness: alert routing, runbooks, backups, budget alerts

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 12 (pre-deploy hardening)

## Context
The gateway had SLOs and alert rules, but:
- **No alert delivery:** nothing routed the alerts to a person.
- **No responder guidance:** an alert gave no hint of what to do.
- **No Postgres backups:** Postgres holds the API keys and the usage log, which is the
  billing record.
- **Late budget notice:** budgets were enforced, but the first sign of an exhausted budget
  was a `429` in an app.

## Options considered
**Alert delivery**
1. **The gateway's own webhook for everything.** It only knows breaker transitions, not
   the SLO burn-rate rules Prometheus evaluates.
2. **Alertmanager** for Prometheus alerts, and the gateway's webhook for events only the
   gateway sees (breakers, budgets). This is the standard split.

**Backups**
1. **The managed database's snapshots.** Best in production, but not available to the
   compose deployment.
2. **A `pg_dump` job in the compose stack,** with retention and a restore drill. Simple and
   portable, and works next to managed snapshots too.

**Budget alerts**
1. **A periodic job** that scans all keys' spend. This means extra load, and alerts lag.
2. **Detect the crossing when a cost is held.** The atomic reservation already returns
   the new month-to-date total, so a threshold between "before" and "after" this hold is
   a crossing. There's no extra Redis call per request.

## Decision
**Alertmanager:**
- It runs in the monitoring profile and routes Prometheus alerts to a Slack-compatible
  webhook (`ALERTMANAGER_SLACK_URL`).
- Pages repeat hourly, tickets every 4 hours.
- Every alert carries a `runbook_url` to its section in the Runbooks wiki page.
- Its config is written at start from the environment, since it can't read variables
  itself.

**Backups:**
- **Job:** a `backup` service runs `pg_dump -Fc` daily. It checks the dump is readable
  before keeping it, retains `BACKUP_KEEP_DAYS` (14), and records `last-success`.
- **Restore:** `restore.sh` replaces the database from a dump.
- **Drill:** `make restore-drill` restores the newest dump into a scratch database and
  compares it with the live one, without touching it.
- **Access:** only the database password reaches the container.

**Budget alerts:**
- **Trigger:** `limits.yaml budget_alerts` (default 50 / 80 / 100%), checked on each hold,
  for keys and teams.
- **Dedupe:** each level alerts once per key or team and month, across the fleet, using a
  Redis `SET NX` that lives 40 days.
- **Delivery:** through the existing alert webhook (`ALERT_WEBHOOK_URL`), plus a log line
  and `gateway_budget_alerts_total{level}`.

## Consequences
- **Holds count toward the alerts:** a level can alert on a request whose final cost turns
  out lower. That's the same view the budget enforces, so the alert is never later than
  the refusal.
- **Backups share the host's disk:** the runbook says to copy the volume off-host. On a
  managed database, prefer its snapshots and keep the drill.
- **Verified locally on the production stack:**
  - a backup, a restore drill and a full restore;
  - `GatewayDown` firing and resolving through Alertmanager to a webhook;
  - budget alerts posting from a real request.
