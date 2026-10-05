"""Phase 6 scenarios. Runs on the host; drives the loadgen container inside the bench
network, injects faults (Toxiproxy, mock control, docker), saves results/<name>.json.

    docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build
    python tests/load/run_bench.py [scenario ...]     # default: all (~35 min)
    python tests/load/report.py                        # → docs/RESULTS.md

Keys created here may only use the bench aliases (the gateway's .env has real provider
keys) and are revoked when the run ends.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent
RESULTS = HERE / "results"
ROOT = HERE.parent.parent
GW, GW2, TOXI, MOCK, PROM = (
    "http://localhost:8000",
    "http://localhost:8001",
    "http://localhost:8474",
    "http://localhost:8080",
    "http://localhost:9090",
)
IN_NET = {"gw": "http://gateway:8000", "gw2": "http://gateway2:8000", "mock": "http://mockllm:8080"}
DOCKER = shutil.which("docker") or "docker"
COMPOSE = [DOCKER, "compose", "-f", "docker-compose.yml", "-f", "docker-compose.bench.yml"]
BENCH_ALIASES = ["bench-fast", "bench-realistic", "bench-long", "bench-ha", "bench-slow"]
CREATED: list[str] = []


def sh(*args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if check and out.returncode:
        raise RuntimeError(f"{args[:3]}: {out.stderr[-500:]}")
    return out.stdout


@functools.cache
def admin() -> dict[str, str]:
    key = sh("exec", "-T", "gateway", "printenv", "GATEWAY_ADMIN_KEY").strip()
    return {"Authorization": f"Bearer {key}"}


def new_key(name: str, **overrides: Any) -> tuple[str, str]:
    body = {
        "name": name,
        "tier": "chaos",
        "allowed_aliases": BENCH_ALIASES,
        "requests_per_minute": 10_000_000,
        "tokens_per_minute": 2_000_000_000,
        "monthly_budget_usd": 1_000,
    } | overrides
    r = httpx.post(f"{GW}/admin/keys", headers=admin(), json=body, timeout=10)
    r.raise_for_status()
    CREATED.append(r.json()["id"])
    return r.json()["key"], r.json()["id"]


def revoke_all() -> None:
    for kid in CREATED:
        httpx.delete(f"{GW}/admin/keys/{kid}", headers=admin(), timeout=10)
    CREATED.clear()


def load(
    name: str,
    *,
    urls: list[str],
    model: str,
    key: str = "",
    stream: bool = False,
    rate: float = 0,
    concurrency: int = 10,
    ramp: float = 0,
    duration: float = 20,
    warmup: float = 3,
    paired: bool = False,
    timeline: bool = False,
    max_tokens: int = 128,
    timeout: float = 120,
) -> dict[str, Any]:
    args = [
        "run",
        "--rm",
        "-T",
        "loadgen",
        "--model",
        model,
        "--duration",
        str(duration),
        "--warmup",
        str(warmup),
        "--max-tokens",
        str(max_tokens),
        "--timeout",
        str(timeout),
        "--label",
        name,
        "--out",
        f"results/raw/{name}.json",
    ]
    for u in urls:
        args += ["--url", u]
    args += ["--stream"] if stream else []
    args += ["--paired"] if paired else []
    args += ["--timeline"] if timeline else []
    args += (
        ["--rate", str(rate)] if rate else ["--concurrency", str(concurrency), "--ramp", str(ramp)]
    )
    sh(*args, env={"LOADGEN_KEY": key})  # the key travels in the environment, not argv
    data: dict[str, Any] = json.loads((RESULTS / "raw" / f"{name}.json").read_text())
    print(f"  {name}: {json.dumps(data['summary'])[:220]}", flush=True)
    return data


def in_background(fn: Callable[[], Any]) -> threading.Thread:
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def toxiproxy(proxy: str, *, enabled: bool | None = None, latency_ms: int | None = None) -> None:
    if enabled is not None:
        httpx.post(f"{TOXI}/proxies/{proxy}", json={"enabled": enabled}).raise_for_status()
    httpx.delete(f"{TOXI}/proxies/{proxy}/toxics/lag")  # idempotent: 404 if absent
    if latency_ms:
        httpx.post(
            f"{TOXI}/proxies/{proxy}/toxics",
            json={
                "name": "lag",
                "type": "latency",
                "stream": "downstream",
                "attributes": {"latency": latency_ms, "jitter": 0},
            },
        ).raise_for_status()


def gateway_metric(name: str, service: str = "gateway") -> float:
    """Sum of a metric's samples from a gateway's internal metrics port."""
    text = sh(
        "exec",
        "-T",
        service,
        "python",
        "-c",
        "import urllib.request;print(urllib.request.urlopen("
        "'http://localhost:9100/metrics').read().decode())",
    )
    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith(name) and not line.startswith("#")
    )


def mock_served(model: str) -> int:
    return int(httpx.get(f"{MOCK}/stats", timeout=5).json().get(model, 0))


def docker_stats(service: str) -> dict[str, str]:
    cid = sh("ps", "-q", service).strip()
    out = subprocess.run(
        [DOCKER, "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", cid],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    cpu, mem = (out.split("|") + ["", ""])[:2]
    return {"cpu": cpu, "mem": mem.split("/")[0].strip()}


def psql(query: str) -> str:
    return sh("exec", "-T", "postgres", "psql", "-U", "gateway", "-tA", "-c", query).strip()


def save(name: str, data: dict[str, Any]) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2))


def restart_gateways() -> None:
    sh("restart", "gateway", "gateway2")
    for url in (GW, GW2):
        for _ in range(60):
            try:
                if httpx.get(f"{url}/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)


# --- analysis (unit-tested in tests/test_bench_analysis.py) ---------------------------


def paired_deltas(
    gw: list[dict[str, Any]], direct: list[dict[str, Any]], key: str
) -> dict[str, float | None]:
    """Per-request differences between two --paired runs (same seed = same mock delays),
    so the mock's own randomness cancels out."""
    by_seq = {r["seq"]: r for r in direct if r.get(key) is not None and r["status"] == 200}
    diffs = sorted(
        (r[key] - by_seq[r["seq"]][key]) * 1000
        for r in gw
        if r.get(key) is not None and r["status"] == 200 and r["seq"] in by_seq
    )
    if not diffs:
        return {"n": 0, "p50": None, "p95": None}
    return {
        "n": len(diffs),
        "p50": round(diffs[len(diffs) // 2], 2),
        "p95": round(diffs[min(len(diffs) - 1, round(0.95 * (len(diffs) - 1)))], 2),
    }


def breaker_stats(reqs: list[dict[str, Any]], fail_at: float, ok_at: float) -> dict[str, Any]:
    """From a timeline: how many requests paid for the dead primary before the breaker
    opened, and when the primary served again after it recovered."""
    reqs = sorted(reqs, key=lambda r: r["t"])
    during = [r for r in reqs if fail_at <= r["t"] < ok_at]
    paid = [r for r in during if r["attempts"] not in ("1", "")]
    back = next(
        (r["t"] for r in reqs if r["t"] >= ok_at and r["provider"].endswith("primary")), None
    )
    return {
        "requests_that_paid": len(paid),
        "failed_attempts": sum(int(r["attempts"]) - 1 for r in paid if r["attempts"]),
        "recover_s": round(back - ok_at, 2) if back is not None else None,
        "client_errors": sum(1 for r in reqs if r["status"] != 200),
    }


def median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


# --- scenarios ---------------------------------------------------------------------


def overhead() -> None:
    """Gateway vs direct to the same mock, paired by seed: both paths see identical
    provider timing, so the difference is the gateway alone."""
    key, _ = new_key("bench-overhead")
    out: dict[str, Any] = {}
    for stream in (False, True):
        for rate in (20, 100):
            tag = f"{'stream' if stream else 'json'}-{rate}rps"
            via = load(
                f"overhead-gw-{tag}",
                urls=[IN_NET["gw"]],
                key=key,
                model="bench-fast",
                stream=stream,
                rate=rate,
                duration=20,
                paired=True,
                timeline=True,
            )
            direct = load(
                f"overhead-direct-{tag}",
                urls=[IN_NET["mock"]],
                model="fast",
                stream=stream,
                rate=rate,
                duration=20,
                paired=True,
                timeline=True,
            )
            metric = "ttft" if stream else "total"
            out[tag] = {
                "gateway": via["summary"],
                "direct": direct["summary"],
                "paired_added_ms": paired_deltas(via["requests"], direct["requests"], metric),
            }
    runs = {}
    for target, urls, model, k in (
        ("gateway", [IN_NET["gw"]], "bench-realistic", key),
        ("direct", [IN_NET["mock"]], "realistic", ""),
    ):
        runs[target] = load(
            f"overhead-realistic-{target}",
            urls=urls,
            model=model,
            key=k,
            stream=True,
            rate=20,
            duration=30,
            paired=True,
            timeline=True,
            max_tokens=200,
        )
    out["realistic-stream-20rps"] = {
        "gateway": runs["gateway"]["summary"],
        "direct": runs["direct"]["summary"],
        "paired_added_ttft_ms": paired_deltas(
            runs["gateway"]["requests"], runs["direct"]["requests"], "ttft"
        ),
        "paired_added_total_ms": paired_deltas(
            runs["gateway"]["requests"], runs["direct"]["requests"], "total"
        ),
    }
    save("overhead", out)


def capacity() -> None:
    """Concurrent long streams on one replica vs the same load direct to the mock."""
    key, _ = new_key("bench-capacity")
    steps = []
    for conc in (50, 100, 200, 400, 800):
        samples: list[dict[str, str]] = []

        def sample(samples: list[dict[str, str]] = samples) -> None:
            for _ in range(5):
                time.sleep(5)
                samples.append(docker_stats("gateway"))

        lag_before = gateway_metric("gateway_event_loop_lag_seconds_sum")
        lag_n_before = gateway_metric("gateway_event_loop_lag_seconds_count")
        t = in_background(sample)
        # client starts ramp over one stream length, so they don't fire in lockstep
        res = load(
            f"capacity-{conc}",
            urls=[IN_NET["gw"]],
            key=key,
            model="bench-long",
            stream=True,
            concurrency=conc,
            ramp=6,
            warmup=8,
            duration=25,
            max_tokens=1000,
        )
        t.join()
        lag_n = gateway_metric("gateway_event_loop_lag_seconds_count") - lag_n_before
        lag = (gateway_metric("gateway_event_loop_lag_seconds_sum") - lag_before) / max(lag_n, 1)
        direct = load(
            f"capacity-direct-{conc}",
            urls=[IN_NET["mock"]],
            model="long",
            stream=True,
            concurrency=conc,
            ramp=6,
            warmup=8,
            duration=25,
            max_tokens=1000,
        )
        cpus = [float(x["cpu"].rstrip("%")) for x in samples if x.get("cpu")]
        steps.append(
            {
                "concurrency": conc,
                **res["summary"],
                "direct": direct["summary"],
                "gateway": {
                    "cpu_peak": f"{max(cpus):.0f}%" if cpus else "–",
                    "mem": samples[-1]["mem"] if samples else "–",
                },
                "mean_loop_lag_ms": round(lag * 1000, 2),
            }
        )
    save("capacity", {"steps": steps})


def scaling() -> None:
    """Max throughput, 1 vs 2 replicas sharing Redis + Postgres (alternating runs), plus
    the bench rig's own ceiling (load generator straight to the mock)."""
    key, _ = new_key("bench-scaling")
    runs: dict[str, list[dict[str, Any]]] = {"one": [], "two": [], "direct": []}
    for i in range(2):
        for name, urls, model, k in (
            ("one", [IN_NET["gw"]], "bench-fast", key),
            ("two", [IN_NET["gw"], IN_NET["gw2"]], "bench-fast", key),
            ("direct", [IN_NET["mock"]], "fast", ""),
        ):
            runs[name].append(
                load(
                    f"scaling-{name}-{i}",
                    urls=urls,
                    key=k,
                    model=model,
                    concurrency=200,
                    ramp=2,
                    warmup=5,
                    duration=20,
                )["summary"]
            )
    save("scaling", runs)


def accuracy() -> None:
    """Rate limits across 2 replicas; budget overshoot under concurrency; usage-log
    completeness; token-estimate error — each from its own key's rows."""
    rpm, seconds = 1200, 20
    key, kid = new_key("bench-rl", requests_per_minute=rpm)
    rl = load(
        "accuracy-ratelimit",
        urls=[IN_NET["gw"], IN_NET["gw2"]],
        key=key,
        model="bench-fast",
        concurrency=100,
        warmup=0,
        duration=seconds,
    )
    expected = rpm + rpm / 60 * seconds
    allowed = rl["summary"]["status"].get("200", 0)

    budget = 0.05
    bkey, bkid = new_key("bench-budget", monthly_budget_usd=budget)
    bd = load(
        "accuracy-budget",
        urls=[IN_NET["gw"], IN_NET["gw2"]],
        key=bkey,
        model="bench-realistic",
        stream=True,
        concurrency=50,
        warmup=0,
        duration=15,
        max_tokens=200,
    )
    time.sleep(3)  # usage writer flush
    keys = httpx.get(f"{GW}/admin/keys", headers=admin()).json()["data"]
    spent = next(k["spent_this_month_usd"] for k in keys if k["id"] == bkid)
    served = bd["summary"]["status"].get("200", 0)
    rows = int(psql(f"select count(*) from usage_log where key_id = '{uuid.UUID(kid)}'"))  # noqa: S608
    ratios = {}
    for name, k in (
        ("bench-fast (max_tokens 128)", kid),
        ("bench-realistic (max_tokens 200)", bkid),
    ):
        ratios[name] = psql(
            "select round(percentile_cont(0.5) within group (order by estimated_tokens::numeric"
            " / nullif(prompt_tokens + completion_tokens, 0))::numeric, 2) from usage_log "  # noqa: S608
            f"where key_id = '{uuid.UUID(k)}' and status = 200"
        )
    save(
        "accuracy",
        {
            "ratelimit": {
                "rpm": rpm,
                "seconds": seconds,
                "replicas": 2,
                "allowed": allowed,
                "expected": expected,
                "error_pct": round(100 * (allowed - expected) / expected, 2),
                "rate_limited": rl["summary"]["status"].get("429", 0),
            },
            "budget": {
                "budget_usd": budget,
                "spent_usd": round(spent, 5),
                "overshoot_pct": round(100 * (spent - budget) / budget, 1),
                "overshoot_requests": (
                    round((spent - budget) / (spent / served), 1) if served else None
                ),
                "served": served,
                "blocked": bd["summary"]["status"].get("429", 0),
                "concurrency": 50,
            },
            "usage_log": {"requests_admitted": allowed, "rows": rows},
            "estimate_over_actual_p50": ratios,
        },
    )


def breaker() -> None:
    """Primary fails ~12 s into the measured run and recovers 25 s later."""
    key, _ = new_key("bench-breaker")
    httpx.post(f"{MOCK}/control", json={"fail": []})
    marks: dict[str, float] = {}

    def chaos() -> None:
        time.sleep(20)  # container start + warm-up + steady traffic
        marks["fail"] = time.time()
        httpx.post(f"{MOCK}/control", json={"fail": ["primary"]})
        time.sleep(25)
        marks["ok"] = time.time()
        httpx.post(f"{MOCK}/control", json={"fail": []})

    t = in_background(chaos)
    res = load(
        "breaker",
        urls=[IN_NET["gw"]],
        key=key,
        model="bench-ha",
        rate=50,
        duration=90,
        timeline=True,
    )
    t.join()
    fail_at = marks["fail"] - res["started_at"]  # fault times on the load generator's clock
    ok_at = marks["ok"] - res["started_at"]
    save(
        "breaker",
        {
            "summary": res["summary"],
            "rate": 50,
            "outage_s": [round(fail_at, 2), round(ok_at, 2)],
            **breaker_stats(res["requests"], fail_at, ok_at),
            "timeline": [
                {"t": round(r["t"], 2), "provider": r["provider"], "attempts": r["attempts"]}
                for r in res["requests"]
            ],
        },
    )


def chaos() -> None:
    """Provider down/slow, rate-limit storm, Redis slow/down, Postgres down."""
    key, _ = new_key("bench-chaos")
    out: dict[str, Any] = {}
    httpx.post(f"{MOCK}/control", json={"fail": ["primary"]})
    out["provider_down"] = load(
        "chaos-provider-down", urls=[IN_NET["gw"]], key=key, model="bench-ha", rate=50, duration=20
    )["summary"]
    httpx.post(f"{MOCK}/control", json={"fail": []})

    # Streams: the first-token timeout (5 s for the bench provider) applies. Non-streaming
    # requests have no first-token budget — the answer only arrives at the end.
    slow = load(
        "chaos-provider-slow",
        urls=[IN_NET["gw"]],
        key=key,
        model="bench-slow",
        stream=True,
        rate=20,
        duration=40,
        warmup=0,
        timeline=True,
    )
    early = sorted(r["total"] for r in slow["requests"] if r["t"] < 5)
    late = sorted(r["total"] for r in slow["requests"] if r["t"] > 30)
    out["provider_slow"] = slow["summary"] | {
        "first_5s_p50_ms": round(1000 * early[len(early) // 2], 1) if early else None,
        "last_10s_p50_ms": round(1000 * late[len(late) // 2], 1) if late else None,
    }

    storm_key, _ = new_key("bench-storm", requests_per_minute=60)
    calm: dict[str, Any] = {}
    t = in_background(
        lambda: calm.update(
            load(
                "chaos-storm-bystander",
                urls=[IN_NET["gw"]],
                key=key,
                model="bench-fast",
                rate=20,
                duration=20,
            )
        )
    )
    storm = load(
        "chaos-storm", urls=[IN_NET["gw"]], key=storm_key, model="bench-fast", rate=300, duration=20
    )
    t.join()
    out["ratelimit_storm"] = {"storm": storm["summary"], "bystander": calm["summary"]}

    for scenario, kwargs in (
        ("redis_slow_200ms", {"latency_ms": 200}),
        ("redis_down", {"enabled": False}),
    ):
        toxiproxy("redis", **kwargs)  # type: ignore[arg-type]
        out[scenario] = load(
            f"chaos-{scenario}",
            urls=[IN_NET["gw"]],
            key=key,
            model="bench-fast",
            rate=50,
            duration=20,
            warmup=0,
        )["summary"]
        toxiproxy("redis", enabled=True)
    time.sleep(6)  # limiter/breaker Redis back-off expires

    # Postgres down. A fresh key, used briefly, then left idle past the 30 s cache TTL —
    # so during the outage every lookup *must* go through stale-if-error.
    pg_key, _ = new_key("bench-pg")
    load(
        "chaos-pg-prime",
        urls=[IN_NET["gw"]],
        key=pg_key,
        model="bench-fast",
        rate=5,
        duration=2,
        warmup=0,
    )
    time.sleep(35)
    stale_before = gateway_metric("gateway_auth_stale_served_total")
    dropped_before = gateway_metric("gateway_usage_log_dropped_total")
    toxiproxy("postgres", enabled=False)
    unknown = httpx.post(
        f"{GW}/v1/chat/completions",
        timeout=10,
        headers={"Authorization": "Bearer gw_" + "x" * 43},
        json={"model": "bench-fast", "messages": [{"role": "user", "content": "x"}]},
    )
    pg = load(
        "chaos-postgres-down",
        urls=[IN_NET["gw"]],
        key=pg_key,
        model="bench-fast",
        rate=50,
        duration=20,
        warmup=0,
    )["summary"]
    pg["unknown_key_status"] = unknown.status_code
    pg["stale_auth_served"] = gateway_metric("gateway_auth_stale_served_total") - stale_before
    toxiproxy("postgres", enabled=True)
    time.sleep(3)
    pg["usage_rows_dropped"] = gateway_metric("gateway_usage_log_dropped_total") - dropped_before
    out["postgres_down"] = pg
    save("chaos", out)


def lifecycle() -> None:
    """SIGTERM with open streams (graceful drain) and config reload under load."""
    key, _ = new_key("bench-lifecycle")
    result: dict[str, Any] = {}
    before = mock_served("long")
    t = in_background(
        lambda: result.update(
            load(
                "lifecycle-restart",
                urls=[IN_NET["gw"]],
                key=key,
                model="bench-long",
                stream=True,
                concurrency=100,
                ramp=1,
                warmup=0,
                duration=2,
                timeout=60,
                timeline=True,
                max_tokens=1000,
            )
        )
    )
    for _ in range(120):  # wait until ≥ 80 streams have reached the provider
        if mock_served("long") - before >= 80:
            break
        time.sleep(0.25)
    sigterm = time.time()
    sh("restart", "gateway")  # SIGTERM → drain (up to 30 s)
    restart_s = time.time() - sigterm
    t.join()
    reqs = result.pop("requests")
    t_sig = sigterm - result["started_at"]
    spanning = [r for r in reqs if r["t"] < t_sig < r["t"] + r["total"]]
    restart_gateways()

    reloads = {"ok": 0, "failed": 0}

    def hammer_reload() -> None:
        end = time.time() + 18
        while time.time() < end:
            r = httpx.post(f"{GW}/admin/reload", headers=admin(), timeout=10)
            reloads["ok" if r.status_code == 200 else "failed"] += 1
            time.sleep(0.25)

    t = in_background(hammer_reload)
    rl = load(
        "lifecycle-reload",
        urls=[IN_NET["gw"]],
        key=key,
        model="bench-fast",
        stream=True,
        rate=50,
        duration=20,
    )
    t.join()
    save(
        "lifecycle",
        {
            "restart_with_open_streams": result["summary"]
            | {
                "restart_took_s": round(restart_s, 1),
                "streams_open_at_sigterm": len(spanning),
                "of_those_completed": sum(
                    1 for r in spanning if r["status"] == 200 and not r["error"]
                ),
            },
            "reload_under_load": rl["summary"] | {"reloads": reloads},
        },
    )


def slo() -> None:
    """Both bench providers fail for ~3.5 minutes; record which alerts fire and when."""
    key, _ = new_key("bench-slo")
    # Alerts already active from earlier scenarios aren't caused by this outage.
    already = {
        a["labels"]["alertname"]: a["state"]
        for a in httpx.get(f"{PROM}/api/v1/alerts").json()["data"]["alerts"]
    }
    httpx.post(f"{MOCK}/control", json={"fail": ["primary", "backup"]})
    seen: dict[str, dict[str, Any]] = {}
    start = time.time()
    t = in_background(
        lambda: load(
            "slo-burn",
            urls=[IN_NET["gw"], IN_NET["gw2"]],
            key=key,
            model="bench-ha",
            rate=30,
            duration=210,
            warmup=0,
        )
    )
    try:
        while t.is_alive():
            time.sleep(10)
            for a in httpx.get(f"{PROM}/api/v1/alerts").json()["data"]["alerts"]:
                name, state = a["labels"]["alertname"], a["state"]
                if seen.get(name, {}).get("state") != state:
                    seen[name] = {
                        "state": state,
                        "at_s": round(time.time() - start),
                        "severity": a["labels"].get("severity"),
                    }
                    print(f"    t+{seen[name]['at_s']}s {name}: {state}", flush=True)
    finally:
        httpx.post(f"{MOCK}/control", json={"fail": []})
        t.join()
    save(
        "slo",
        {
            "alerts": {k: v for k, v in seen.items() if k not in already},
            "already_active_before": already,
        },
    )


SCENARIOS = {
    f.__name__: f for f in (overhead, capacity, scaling, accuracy, breaker, chaos, lifecycle, slo)
}

if __name__ == "__main__":
    try:
        for name in sys.argv[1:] or SCENARIOS:
            print(f"== {name}", flush=True)
            restart_gateways()
            SCENARIOS[name]()
    finally:
        revoke_all()
