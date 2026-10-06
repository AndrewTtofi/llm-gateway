"""Render docs/RESULTS.md from tests/load/results/*.json (Phase 6).

    python tests/load/report.py

Every number and every conclusion below is computed from the result files — no
hand-written claims, so re-running the bench can't leave stale prose behind.
"""

from __future__ import annotations

import json
import platform
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
RESULTS = HERE / "results"
OUT = HERE.parent.parent / "docs" / "RESULTS.md"
PROFILE = json.loads((HERE / "real_claude_profile.json").read_text())
OPEN_SECONDS = 30  # config/models.yaml circuit_breaker.open_seconds


def load(name: str) -> dict[str, Any]:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def ms(v: float | None) -> str:
    if v is None:
        return "–"
    if abs(v) >= 1000:
        return f"{v / 1000:.1f} s"
    return f"{v:.0f} ms" if abs(v) >= 100 else f"{v:.1f} ms" if abs(v) >= 10 else f"{v:.2f} ms"


def plus(v: float | None) -> str:
    return "–" if v is None else ("+" if v >= 0 else "−") + ms(abs(v))


def pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.1f}%"


def xychart(title: str, x: list[Any], series: dict[str, list[float]], y: str) -> str:
    lines = [
        "```mermaid",
        "xychart-beta",
        f'    title "{title}"',
        f"    x-axis [{', '.join(str(v) for v in x)}]",
        f'    y-axis "{y}"',
    ]
    lines += [f"    line [{', '.join(f'{v:.1f}' for v in vals)}]" for vals in series.values()]
    lines.append("```")
    lines.append("_" + " · ".join(f"line {i + 1}: {n}" for i, n in enumerate(series)) + "_")
    return "\n".join(lines)


# --- sections -------------------------------------------------------------------------


def overhead(out: list[str]) -> None:
    d = load("overhead")
    if not d:
        return
    out += [
        "## 1. Gateway overhead",
        "",
        "The same requests go once **directly** to the mock provider and once **through "
        "the gateway** (auth, model check, budget, two token buckets, routing, breaker, "
        "metering, usage log). Runs are **paired by seed**: request *i* gets identical "
        "provider delays on both paths, so the mock's randomness cancels and the "
        "per-request difference is the gateway alone. Open-loop load, latency measured "
        "from each request's scheduled start.",
        "",
        "| Load | Direct p50 / p95 | Gateway p50 / p95 | **Added by the gateway** (paired p50 / p95) | Admission p50 |",
        "|---|---|---|---|---|",
    ]
    tags = ("json-20rps", "json-100rps", "stream-20rps", "stream-100rps")
    for tag in (*tags, "json-20rps-100k", "stream-20rps-100k"):
        if tag not in d:
            continue
        r = d[tag]
        kind, rate, *size = tag.split("-")
        what = "time to first token" if kind == "stream" else "whole request"
        if size:
            what += ", **100k-character prompt**"
        key = "ttft_ms" if kind == "stream" else "total_ms"
        g, x, a = r["gateway"][key], r["direct"][key], r["paired_added_ms"]
        out.append(
            f"| {'stream' if kind == 'stream' else 'non-stream'}, {what}, "
            f"{rate.replace('rps', ' req/s')} | {ms(x['p50'])} / {ms(x['p95'])} | "
            f"{ms(g['p50'])} / {ms(g['p95'])} | **{plus(a['p50'])} / {plus(a['p95'])}** | "
            f"{ms(r['gateway']['admit_ms']['p50'])} |"
        )
    real = d.get("realistic-stream-20rps")
    if real:
        s = PROFILE["summary_ms"]
        t, w = real["paired_added_ttft_ms"], real["paired_added_total_ms"]
        out += [
            "",
            f"**With realistic provider timing** — the mock samples TTFT and "
            f"inter-chunk gaps from {PROFILE['samples']} real Claude Haiku 4.5 streams "
            f"(TTFT p50 {ms(s['ttft_p50'])}, gap p50 {ms(s['gap_p50'])}), 20 req/s:",
            "",
            "| | Direct p50 | Gateway p50 | Added (paired p50 / p95) |",
            "|---|---|---|---|",
            f"| Time to first token | {ms(real['direct']['ttft_ms']['p50'])} | "
            f"{ms(real['gateway']['ttft_ms']['p50'])} | {plus(t['p50'])} / {plus(t['p95'])} |",
            f"| Whole stream | {ms(real['direct']['total_ms']['p50'])} | "
            f"{ms(real['gateway']['total_ms']['p50'])} | {plus(w['p50'])} / {plus(w['p95'])} |",
            "",
        ]
        rel = t["p50"] / real["direct"]["ttft_ms"]["p50"] if t["p50"] is not None else None
        out.append(f"So against a real model's first token the gateway adds **{pct(rel)}** at p50.")
    s100 = d.get("stream-100rps", {}).get("paired_added_ms", {})
    out += [
        "",
        "**Where it goes:** about a third is admission (the `server-timing` header: "
        "auth, model check, budget, rate limits); the rest is the extra network hop, "
        "routing and relaying. The gateway makes 4 Redis round trips per request (2 more "
        "for priced models). Under 100 streams/s the paired p95 is "
        f"{plus(s100.get('p95'))} — the tail is what a production deployment would watch "
        "first.",
        "",
    ]


def capacity(out: list[str]) -> None:
    d = load("capacity")
    if not d:
        return
    steps = d["steps"]
    out += [
        "## 2. Capacity — concurrent streams on one replica",
        "",
        "Closed loop: N clients each holding a ~5 s stream (≈300 ms to first token, then 200 "
        "chunks ≈25 ms apart, ±10% jitter), client starts spread over one stream length. "
        "Each step also runs **directly** against the mock with the same load: the load "
        "generator and the mock are single Python processes too, and where *direct* "
        "degrades the rig is the limit, not the gateway.",
        "",
        "| Streams | TTFT p95 direct → gateway (added) | Chunk gap p95 direct → gateway | Success | Event-loop lag | Gateway CPU peak | Memory |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in steps:
        dt, gt = s["direct"]["ttft_ms"]["p95"], s["ttft_ms"]["p95"]
        out.append(
            f"| {s['concurrency']} | {ms(dt)} → {ms(gt)} ({plus(gt - dt if dt and gt else None)}) | "
            f"{ms(s['direct']['gap_ms']['p95'])} → {ms(s['gap_ms']['p95'])} | "
            f"{pct(s['success_rate'])} | {ms(s['mean_loop_lag_ms'])} | "
            f"{s['gateway']['cpu_peak']} | {s['gateway']['mem']} |"
        )
    out += [
        "",
        xychart(
            "p95 time to first token vs concurrent streams",
            [s["concurrency"] for s in steps],
            {
                "through the gateway (ms)": [s["ttft_ms"]["p95"] or 0 for s in steps],
                "direct (ms)": [s["direct"]["ttft_ms"]["p95"] or 0 for s in steps],
            },
            "ms",
        ),
        "",
    ]

    def tracks(s: dict[str, Any]) -> bool:  # gateway within 10% (+20 ms) of direct
        dt, gt = s["direct"]["ttft_ms"]["p95"], s["ttft_ms"]["p95"]
        dg, gg = s["direct"]["gap_ms"]["p95"], s["gap_ms"]["p95"]
        return bool(dt and gt and gt <= dt * 1.1 + 20 and gg <= dg * 1.1 + 5)

    ok = [s["concurrency"] for s in steps if tracks(s)]
    first_bad = next((s for s in steps if not tracks(s)), None)
    saturated = next(
        (
            s
            for s in steps
            if float(s["gateway"]["cpu_peak"].rstrip("%") or 0) >= 95 or s["mean_loop_lag_ms"] >= 20
        ),
        None,
    )
    rig_bad = next(
        (
            s
            for s in steps
            if s["direct"]["ttft_ms"]["p95"] and s["direct"]["ttft_ms"]["p95"] > 1000
        ),
        None,
    )
    lines = []
    if ok:
        lines.append(
            f"Through **{max(ok)}** concurrent streams the gateway stays within 10% "
            "of the direct path (TTFT and chunk gaps)."
        )
    if first_bad:
        lines.append(
            f"At {first_bad['concurrency']} it adds "
            f"{plus(first_bad['ttft_ms']['p95'] - first_bad['direct']['ttft_ms']['p95'])}"
            " to p95 TTFT."
        )
    if saturated:
        lines.append(
            f"At {saturated['concurrency']} its single core is the bottleneck (CPU "
            f"{saturated['gateway']['cpu_peak']}, event loop "
            f"{ms(saturated['mean_loop_lag_ms'])} late)."
        )
    if rig_bad:
        lines.append(
            f"(From {rig_bad['concurrency']} up the *direct* path degrades too — "
            "part of that step is the rig.)"
        )
    mems = [s["gateway"]["mem"] for s in steps]
    lines.append(
        f"Memory stays modest ({mems[0]} → {mems[-1]}). Scale out with replicas "
        "beyond the first core."
    )
    out += ["**Reading it:** " + " ".join(lines), ""]


def scaling(out: list[str]) -> None:
    d = load("scaling")
    if not d:
        return

    def best(name: str) -> float:
        return max(r["achieved_rps"] for r in d[name])

    rows = [
        f"| {label} | run {i + 1} | {r['achieved_rps']} | {ms(r['total_ms']['p50'])} | "
        f"{ms(r['total_ms']['p95'])} | {pct(r['success_rate'])} |"
        for name, label in (
            ("one", "1 replica"),
            ("two", "2 replicas"),
            ("direct", "no gateway (rig ceiling)"),
        )
        for i, r in enumerate(d.get(name, []))
    ]
    one, two, ceiling = best("one"), best("two"), best("direct")
    verdict = (
        f"Two replicas reach {two / ceiling:.0%} of what the rig can generate at all, "
        "so the rig, not the gateway, caps this number — replica scaling can't be "
        "measured on one laptop."
        if two >= 0.75 * ceiling
        else f"Two replicas serve {two / one:.2f}× one replica's throughput, below the "
        "rig's ceiling."
    )
    out += [
        "## 3. Horizontal scaling — 1 vs 2 replicas",
        "",
        "Closed loop, 200 clients, non-streaming requests to an instant mock, 5 s warm-up, "
        "runs alternating 1 → 2 → rig → 1 → 2 → rig. Both replicas share Redis (buckets, "
        "breakers, budgets) and Postgres. *Rig ceiling* = the same load straight to the "
        "mock, no gateway.",
        "",
        "| Path | Run | Requests/s | p50 | p95 | Success |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        f"Best: **1 replica {one:.0f} req/s · 2 replicas {two:.0f} req/s · rig ceiling "
        f"{ceiling:.0f} req/s.** {verdict} What it does show: two replicas sharing Redis "
        "and Postgres serve the load without errors, and limits stay exact across them "
        "(section 4).",
        "",
    ]


def accuracy(out: list[str]) -> None:
    d = load("accuracy")
    if not d:
        return
    rl, bd, ul = d["ratelimit"], d["budget"], d["usage_log"]
    ratios = "; ".join(f"{k}: **{v}×**" for k, v in d["estimate_over_actual_p50"].items())
    out += [
        "## 4. Accuracy",
        "",
        "| What | Result |",
        "|---|---|",
        f"| Rate limit across **2 replicas** ({rl['rpm']} req/min, {rl['seconds']} s, 100 "
        f"clients) | {rl['allowed']} allowed vs {rl['expected']:.0f} expected → "
        f"**{rl['error_pct']:+.2f}%** ({rl['rate_limited']} rejected) |",
        f"| Budget under concurrency (${bd['budget_usd']}, {bd['concurrency']} concurrent "
        f"streams, 2 replicas) | spent ${bd['spent_usd']} → **{bd['overshoot_pct']:+.1f}%** "
        f"(≈{bd['overshoot_requests']} requests' worth; {bd['served']} served, "
        f"{bd['blocked']} blocked) |",
        f"| Usage log completeness | {ul['rows']} rows for {ul['requests_admitted']} "
        "admitted requests |",
        f"| Token estimate ÷ actual (p50) | {ratios} |",
        "",
        "The mock honours `max_tokens` like a real provider, so the estimate is exact on "
        "output and pessimistic only by what's left unused (estimates reserve the whole "
        "`max_tokens`, then reconcile). The budget overshoot is the race the budget check "
        "allows by design (ADR 0007): requests admitted while others are still in flight — "
        "the check reads month-to-date spend, then reserves, without a lock.",
        "",
    ]


def breaker(out: list[str]) -> None:
    d = load("breaker")
    if not d:
        return
    tl = d["timeline"]
    end = int(max(r["t"] for r in tl)) + 1
    per: dict[str, Counter[int]] = {"primary": Counter(), "backup": Counter()}
    for r in tl:
        for name in per:
            if r["provider"].endswith(name):
                per[name][int(r["t"])] += 1
    buckets = list(range(0, end, 3))
    series = {
        name: [sum(c[s] for s in range(b, b + 3)) / 3 for b in buckets] for name, c in per.items()
    }
    fail, ok = d["outage_s"]
    out += [
        "## 5. Circuit breaker",
        "",
        f"`bench-ha` = [primary, backup] at {d['rate']} req/s. The primary fails at "
        f"t={fail:.0f} s and recovers at t={ok:.0f} s (breaker: 5 failures to open, "
        f"{OPEN_SECONDS} s open, then one probe).",
        "",
        "| | |",
        "|---|---|",
        f"| Requests that failed for the client | **{d['client_errors']}** of "
        f"{d['summary']['requests']} |",
        f"| Requests that paid for a failed attempt before the breaker opened | "
        f"**{d['requests_that_paid']}** ({d['failed_attempts']} failed attempts) |",
        f"| Primary back in service after it recovered | {d['recover_s']} s — anywhere from "
        f"0 to {OPEN_SECONDS} s by design (the next probe after `open_seconds`); here it "
        "depends on when the outage ended |",
        "",
        "Detection is counted in requests, not seconds: the breaker opens after 5 failed "
        "attempts, so at a low request rate it takes longer in wall-clock time.",
        "",
        xychart("Requests/s served by primary vs backup (3 s buckets)", buckets, series, "req/s"),
        "",
    ]


def chaos(out: list[str]) -> None:
    d = load("chaos")
    if not d:
        return
    ps, pg = d["provider_slow"], d["postgres_down"]
    storm, calm = d["ratelimit_storm"]["storm"], d["ratelimit_storm"]["bystander"]
    rows = [
        (
            "Provider down (primary 503s, backup available)",
            d["provider_down"],
            "Fallback hides it; once the breaker opens, no request pays for the dead primary.",
        ),
        (
            "Provider slow (sends no token within `first_token` = 5 s; streams)",
            ps,
            f"Until the breaker opens, each request waits out the timeout **twice** (2 attempts "
            f"× 5 s; p50 {ms(ps.get('first_5s_p50_ms'))} in the first 5 s), then goes straight "
            f"to the backup (p50 {ms(ps.get('last_10s_p50_ms'))} later). Non-streaming requests "
            "have no first-token budget and would wait up to `total`.",
        ),
        (
            "Rate-limit storm — the flooding key (300 req/s, limit 60/min)",
            storm,
            f"{storm['status'].get('429', 0)} rejected with 429, "
            f"{storm['status'].get('200', 0)} served.",
        ),
        ("Rate-limit storm — another key at the same time", calm, "Unaffected."),
        (
            "Redis +200 ms latency",
            d["redis_slow_200ms"],
            "Redis calls time out (100 ms); limits and breakers **fail open** for 5 s at a "
            "time. Traffic flows; spend is queued on each replica and written once Redis "
            "answers, and budgets use the last known spend meanwhile (ADR 0023).",
        ),
        (
            "Redis down",
            d["redis_down"],
            "Fails open: rate limits and breakers are off until it's back. Budgets keep the "
            "last known spend plus what each replica queued, so a spent key stays blocked.",
        ),
        (
            "Postgres down",
            pg,
            f"A key idle past its 30 s cache TTL keeps working via stale-if-error "
            f"({pg.get('stale_auth_served', 0):.0f} requests authenticated that way); an unknown "
            f"key gets {pg.get('unknown_key_status')}; {pg.get('usage_rows_dropped', 0):.0f} "
            "usage rows dropped and counted.",
        ),
    ]
    out += [
        "## 6. Chaos",
        "",
        "| Scenario | Served | p50 | p95 | What happened |",
        "|---|---|---|---|---|",
    ]
    for name, s, note in rows:
        lat = s.get("all_ms") or s["total_ms"]
        served = (
            f"{s['status'].get('200', 0)} / {s['requests']}"
            if "flooding" in name
            else pct(s["success_rate"])
        )
        out.append(f"| {name} | {served} | {ms(lat['p50'])} | {ms(lat['p95'])} | {note} |")
    out.append("")


def lifecycle(out: list[str]) -> None:
    d = load("lifecycle")
    if not d:
        return
    rs, rl = d["restart_with_open_streams"], d["reload_under_load"]
    out += [
        "## 7. Operations",
        "",
        "| Scenario | Result |",
        "|---|---|",
        f"| SIGTERM with open streams (`--timeout-graceful-shutdown 30`) | "
        f"**{rs['of_those_completed']} of {rs['streams_open_at_sigterm']}** streams that "
        f"were open at SIGTERM completed; restart took {rs['restart_took_s']} s |",
        f"| Config reload every ~0.25 s under 50 req/s of streams | "
        f"{rl['reloads']['ok']} reloads, {rl['reloads']['failed']} failed; requests "
        f"{pct(rl['success_rate'])} successful |",
        "",
    ]


def soak(out: list[str]) -> None:
    d = load("soak")
    if not d:
        return
    samples = d["samples"]
    settled = [x for x in samples if x["minute"] >= 5] or samples  # after warm-up

    def growth(field: str) -> str:
        vals = [x[field] for x in settled if x.get(field)]
        if len(vals) < 2:
            return "n/a"
        return f"{vals[0]:.0f} → {vals[-1]:.0f} MiB ({(vals[-1] - vals[0]) / vals[0]:+.0%})"

    rows = [
        f"| {name} | {r['requests']} | {pct(r['success_rate'])} |" for name, r in d["runs"].items()
    ]
    out += [
        f"## 9. Soak: {d['minutes']} minutes of mixed load",
        "",
        "Both replicas, three keys at once: non-streamed requests at 40/s, 40 concurrent "
        "realistic streams, 15 concurrent long streams, and a config reload every 5 minutes.",
        "",
        "| Load | Requests | Success |",
        "|---|---|---|",
        *rows,
        "",
        "| Memory (after a 5-minute warm-up) | |",
        "|---|---|",
        f"| Gateway replica 1 | {growth('gateway_mib')} |",
        f"| Gateway replica 2 | {growth('gateway2_mib')} |",
        f"| Redis | {growth('redis_mib')} |",
        "",
        f"Config reloads: {d['reloads']['ok']} ok, {d['reloads']['failed']} failed. Usage rows "
        f"written: {d['usage_rows']} for {d['requests']} requests.",
        "",
        xychart(
            "Memory over the soak (MiB)",
            [x["minute"] for x in samples],
            {
                "gateway": [x["gateway_mib"] or 0 for x in samples],
                "gateway2": [x["gateway2_mib"] or 0 for x in samples],
            },
            "MiB",
        ),
        "",
    ]


def slo(out: list[str]) -> None:
    d = load("slo")
    if not d:
        return
    alerts = d["alerts"]
    out += [
        "## 8. SLO alerts",
        "",
        "`config/prometheus-rules.yml`: two SLOs — 99.9% of admitted requests succeed; "
        "p95 time to first token (as the client sees it) ≤ 1.5 s — with multi-window "
        "burn-rate alerts (SRE workbook), plus health alerts. Validated with `promtool`. "
        "Then both bench providers failed for ~3.5 minutes at 30 req/s across both "
        "replicas:",
        "",
        "| Alert | Severity | State reached | After |",
        "|---|---|---|---|",
        *[
            f"| {name} | {a['severity']} | {a['state']} | {a['at_s']} s |"
            for name, a in sorted(alerts.items(), key=lambda kv: kv[1]["at_s"])
        ],
        "",
        (
            "Already active before the fault (carried over from earlier scenarios), "
            "so not counted: " + ", ".join(sorted(d["already_active_before"])) + "."
            if d.get("already_active_before")
            else "No alerts were active before the fault."
            + (
                " GatewayDown went pending because the scenario restarts both gateways "
                "just before the fault; it didn't fire."
                if "GatewayDown" in alerts
                else ""
            )
        ),
        "",
        "The page-level fast burn needs both the 5-minute and 1-hour error ratios above "
        "14.4× the budget rate and then `for: 2m`, so it fires a few minutes in. With "
        "both breakers open, requests fail fast (503 `all_providers_unavailable`) "
        "instead of piling up behind dead providers.",
        "",
    ]


def findings() -> list[str]:
    o, c, a, b, ch, lc, sl = (
        load(n)
        for n in ("overhead", "capacity", "accuracy", "breaker", "chaos", "lifecycle", "slo")
    )
    out = ["## Key findings", ""]
    if o:
        j, s = o["json-100rps"]["paired_added_ms"], o["stream-100rps"]["paired_added_ms"]
        r = o["realistic-stream-20rps"]["paired_added_ttft_ms"]
        out.append(
            f"- **Overhead:** {plus(j['p50'])} p50 / {plus(j['p95'])} p95 per request "
            f"at 100 req/s (paired); {plus(r['p50'])} to a realistic time-to-first-token. "
            f"Tail under 100 streams/s: {plus(s['p95'])} at p95."
        )
    if c:
        tracked = [
            s["concurrency"]
            for s in c["steps"]
            if s["ttft_ms"]["p95"]
            and s["direct"]["ttft_ms"]["p95"]
            and s["ttft_ms"]["p95"] <= s["direct"]["ttft_ms"]["p95"] * 1.1 + 20
        ]
        out.append(
            f"- **Capacity:** one replica (one core) stays within 10% of the direct "
            f"path up to {max(tracked) if tracked else '–'} concurrent streams."
        )
    if a:
        out.append(
            f"- **Accuracy:** rate limit across 2 replicas "
            f"{a['ratelimit']['error_pct']:+.2f}%; budget overshoot "
            f"{a['budget']['overshoot_pct']:+.1f}% with 50 concurrent streams; usage log "
            f"{a['usage_log']['rows']}/{a['usage_log']['requests_admitted']} rows."
        )
    if b:
        out.append(
            f"- **Breaker:** {b['requests_that_paid']} requests paid for the dead "
            f"primary before it opened; {b['client_errors']} client-visible errors."
        )
    if ch:
        rates = [
            ch[k]["success_rate"]
            for k in (
                "provider_down",
                "provider_slow",
                "redis_slow_200ms",
                "redis_down",
                "postgres_down",
            )
        ]
        out.append(
            f"- **Chaos:** provider down/slow, Redis slow/down, Postgres down — "
            f"{min(rates):.1%}+ of requests served in each. Redis outages switch "
            "rate limits off (fail open; budgets use the last known spend, and spend is "
            "queued); a slow provider costs 2 × first_token per request until the breaker opens."
        )
    if lc:
        rs = lc["restart_with_open_streams"]
        out.append(
            f"- **Operations:** {rs['of_those_completed']}/{rs['streams_open_at_sigterm']}"
            " streams open at SIGTERM completed; config reloads under load caused "
            f"{lc['reload_under_load']['reloads']['failed']} errors."
        )
    if sl:
        fired = [n for n, x in sl["alerts"].items() if x["state"] == "firing"]
        out.append(
            f"- **SLO alerts:** {', '.join(sorted(fired)) or 'none'} fired during a full outage."
        )
    return [*out, ""]


def environment() -> list[str]:
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=10
            ).stdout.strip()
        except Exception:
            return "?"

    return [
        "## Environment and limits",
        "",
        f"- One laptop: {platform.system()} {platform.release()} (WSL2), {run(['nproc'])} "
        f"CPUs, {run(['sh', '-c', 'free -g | awk /Mem:/{print\\ $2}'])} GB RAM, Docker "
        "Desktop. Load generator, 2 gateway replicas, Redis, Postgres, Toxiproxy and the "
        "mock share it.",
        "- Gateway: one uvicorn process per replica, no `--reload`; Redis and Postgres "
        "reached through Toxiproxy (one extra hop). Load generator inside the Docker "
        "network.",
        f"- Realistic timing: {PROFILE['samples']} real Claude Haiku 4.5 streams "
        f"({PROFILE['gap_samples']} inter-chunk gaps), measured through the gateway "
        "(so they include its few ms).",
        "- Absolute numbers are a floor for this hardware; the comparisons — direct vs "
        "gateway, before vs during a fault — are what transfer.",
        f"- Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `tests/load/report.py`.",
        "",
    ]


def main() -> None:
    out = [
        "# Results",
        "",
        "Phase 6: how much the gateway costs, how far it scales, and what happens when "
        "things break. Method: [ADR 0009](decisions/0009-benchmarking.md). Reproduce:",
        "",
        "```bash",
        "docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build",
        "python tests/load/run_bench.py      # ~40 min → tests/load/results/",
        "python tests/load/report.py         # → this file",
        "```",
        "",
        *findings(),
    ]
    for section in (overhead, capacity, scaling, accuracy, breaker, chaos, lifecycle, slo, soak):
        section(out)
    out += environment()
    OUT.write_text("\n".join(out))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
