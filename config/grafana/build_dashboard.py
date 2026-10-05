"""Generates config/grafana/dashboards/llm-gateway.json. Run: python config/grafana/build_dashboard.py

The dashboard is code: edit this file, regenerate, commit both.
"""

import json
from pathlib import Path
from typing import Any

PROM = {"type": "prometheus", "uid": "gw-prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "gw-postgres"}
RATE = "[$__rate_interval]"
# provider = the part of target before "/"
BY_PROVIDER = 'label_replace({inner}, "provider", "$1", "target", "([^/]+)/.*")'

panels: list[dict[str, Any]] = []
_id = 0


def panel(kind: str, title: str, w: int, h: int, x: int, y: int, targets: list[dict[str, Any]],
          ds: dict[str, str] = PROM, unit: str | None = None, desc: str = "",
          **extra: Any) -> None:
    global _id
    _id += 1
    fc: dict[str, Any] = {"defaults": {}, "overrides": []}
    if unit:
        fc["defaults"]["unit"] = unit
    for i, t in enumerate(targets):
        t.setdefault("refId", chr(65 + i))
        t["datasource"] = ds
    panels.append({"id": _id, "type": kind, "title": title, "description": desc,
                   "gridPos": {"w": w, "h": h, "x": x, "y": y}, "datasource": ds,
                   "targets": targets, "fieldConfig": fc, **extra})


def prom(expr: str, legend: str = "") -> dict[str, Any]:
    return {"expr": expr, "legendFormat": legend, "range": True}


def sql(query: str, fmt: str = "table") -> dict[str, Any]:
    return {"rawSql": query, "format": fmt, "rawQuery": True, "editorMode": "code"}


total = f"sum(rate(gateway_requests_total{RATE}))"

# --- row 1: headline numbers ---
panel("stat", "Requests / s", 4, 4, 0, 0, [prom(total)], unit="reqps")
panel("stat", "Fallback rate", 4, 4, 4, 0,
      [prom(f"(sum(rate(gateway_fallbacks_total{RATE})) or vector(0)) / {total}")],
      unit="percentunit",
      desc="Share of requests served by a target other than the alias's first choice.")
# Gateway/upstream failures only: 5xx, and 200 streams that failed in-band ("200:<code>")
# except the client hanging up. Client errors (4xx, 499) aren't the gateway's error rate.
GATEWAY_ERRORS = 'status=~"5.*|200:.*",status!~".*client_disconnected"'
panel("stat", "Error rate", 4, 4, 8, 0,
      # `or vector(0)`: with no errors at all the selector matches nothing → "No data".
      [prom(f"(sum(rate(gateway_requests_total{{{GATEWAY_ERRORS}}}{RATE})) or vector(0))"
            f" / {total}")],
      unit="percentunit", desc="5xx and in-band stream failures. Excludes client errors (4xx).")
panel("stat", "p95 latency", 4, 4, 12, 0, [prom(
    f"histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket{RATE})))")],
    unit="s")
panel("stat", "Spend this month", 4, 4, 16, 0, [sql(
    "SELECT coalesce(sum(cost_usd), 0) AS spend FROM usage_log "
    "WHERE created_at >= date_trunc('month', now())")], ds=PG, unit="currencyUSD")
panel("stat", "Usage rows dropped", 4, 4, 20, 0,
      [prom("sum(gateway_usage_log_dropped_total) or vector(0)")],
      desc="Usage-log rows lost because Postgres was slow or down. Should be 0.")

# --- row 2: latency ---
panel("timeseries", "p95 latency per provider", 12, 8, 0, 4, [prom(
    "histogram_quantile(0.95, sum by (le, provider) ("
    + BY_PROVIDER.format(inner=f"rate(gateway_request_duration_seconds_bucket{RATE})") + "))",
    "{{provider}}")], unit="s", desc="Whole request; for streams, until the last token.")
panel("timeseries", "p95 time to first token (client-side, and per serving target)", 12, 8, 12, 4, [prom(
    f"histogram_quantile(0.95, sum by (le) (rate(gateway_ttft_e2e_seconds_bucket{RATE})))",
    "client (end to end)"), prom(
    f"histogram_quantile(0.95, sum by (le, target) (rate(gateway_ttft_seconds_bucket{RATE})))",
    "{{target}}")], unit="s", desc="Streams: how long users stare at an empty response.")

# --- row 3: traffic & reliability ---
panel("timeseries", "Requests / s by target and status", 12, 8, 0, 12, [prom(
    f"sum by (target, status) (rate(gateway_requests_total{RATE}))", "{{target}} {{status}}")],
    unit="reqps")
panel("timeseries", "Fallback rate by alias", 12, 8, 12, 12, [prom(
    f"sum by (alias) (rate(gateway_fallbacks_total{RATE})) / "
    f"sum by (alias) (rate(gateway_requests_total{RATE}))", "{{alias}}")], unit="percentunit")
panel("state-timeline", "Circuit breakers", 12, 7, 0, 20,
      [prom("max by (target) (gateway_circuit_state)", "{{target}}")],
      desc="0 closed · 1 half-open (probing) · 2 open (target skipped).",
      options={"showValue": "never"})
panels[-1]["fieldConfig"]["defaults"]["mappings"] = [{"type": "value", "options": {
    "0": {"text": "closed", "color": "green"}, "1": {"text": "half-open", "color": "yellow"},
    "2": {"text": "open", "color": "red"}}}]
panel("timeseries", "Upstream attempts by outcome", 12, 7, 12, 20, [prom(
    f"sum by (target, outcome) (rate(gateway_upstream_attempts_total{RATE}))",
    "{{target}} {{outcome}}")], unit="reqps",
    desc="Every upstream call, including retries that preceded a fallback.")

# --- row 4: tokens, cost, limits ---
panel("timeseries", "Tokens / min by target", 8, 8, 0, 27, [prom(
    f'sum by (target, kind) (rate(gateway_tokens_total{{kind!="cached"}}{RATE})) * 60',
    "{{target}} {{kind}}")], unit="short")
panel("timeseries", "Spend / hour by target", 8, 8, 8, 27, [prom(
    f"sum by (target) (rate(gateway_cost_usd_total{RATE})) * 3600", "{{target}}")],
    unit="currencyUSD")
panel("timeseries", "Rejected requests by reason", 8, 8, 16, 27, [prom(
    f"sum by (reason) (rate(gateway_rejected_total{RATE}))", "{{reason}}")], unit="reqps",
    desc="Before routing: bad key, model not allowed, rate limit, budget.")

# --- row 5: per key (Postgres) ---
panel("table", "Spend per key — this month", 12, 9, 0, 35, [sql(
    "SELECT coalesce(k.name, u.key_prefix) AS key, u.key_prefix AS prefix, "
    "count(*) AS requests, sum(u.prompt_tokens + u.completion_tokens) AS tokens, "
    "round(sum(coalesce(u.cost_usd, 0))::numeric, 4) AS spend_usd, "
    "round(avg(u.latency_ms)) AS avg_ms, "
    "round(100.0 * avg(CASE WHEN u.fallback THEN 1 ELSE 0 END), 1) AS fallback_pct "
    "FROM usage_log u LEFT JOIN api_keys k ON k.id = u.key_id "
    "WHERE u.created_at >= date_trunc('month', now()) "
    "GROUP BY 1, 2 ORDER BY spend_usd DESC")], ds=PG,
    desc="Exact, from the usage log (ADR 0008).")
panel("timeseries", "Spend per key over time", 12, 9, 12, 35, [sql(
    "SELECT $__timeGroupAlias(u.created_at, $__interval), coalesce(k.name, u.key_prefix) AS metric, "
    "sum(coalesce(u.cost_usd, 0)) AS value "
    "FROM usage_log u LEFT JOIN api_keys k ON k.id = u.key_id "
    "WHERE $__timeFilter(u.created_at) GROUP BY 1, 2 ORDER BY 1", "time_series")], ds=PG,
    unit="currencyUSD")

dashboard = {
    "uid": "llm-gateway",
    "title": "LLM Gateway",
    "tags": ["llm-gateway"],
    "timezone": "browser",
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "schemaVersion": 39,
    "editable": False,
    "panels": panels,
}
out = Path(__file__).parent / "dashboards" / "llm-gateway.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(panels)} panels)")
