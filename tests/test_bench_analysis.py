"""The maths behind docs/RESULTS.md (tests/load): percentiles, success rules, pairing,
breaker detection/recovery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "load"))

import loadgen  # noqa: E402
import run_bench  # noqa: E402


def test_nearest_rank_percentile() -> None:
    xs = [float(i) for i in range(1, 101)]
    assert loadgen.pct(xs, 50) == 51.0 and loadgen.pct(xs, 95) == 95.0
    assert loadgen.pct([], 50) is None


def test_truncated_or_errored_streams_are_not_successes() -> None:
    ok = loadgen.Result(t=0, status=200, total=1, done=True)
    truncated = loadgen.Result(t=0, status=200, total=1, error="truncated")
    in_band = loadgen.Result(t=0, status=200, total=1, error="in_band")
    s = loadgen.summary([ok, truncated, in_band], window=1)
    assert s["ok"] == 1 and s["success_rate"] == round(1 / 3, 4)
    assert s["status"] == {"200": 1, "200:truncated": 1, "200:in_band": 1}


def test_rate_is_measured_over_the_arrival_window_not_the_drain() -> None:
    results = [loadgen.Result(t=i / 10, status=200, total=30) for i in range(100)]
    assert loadgen.summary(results, window=10)["achieved_rps"] == 10.0


def test_paired_deltas_compare_the_same_seed() -> None:
    gw = [{"seq": i, "status": 200, "ttft": 0.5 + i / 100 + 0.004} for i in range(10)]
    direct = [{"seq": i, "status": 200, "ttft": 0.5 + i / 100} for i in range(10)]
    d = run_bench.paired_deltas(gw, direct, "ttft")
    assert d["n"] == 10 and abs(d["p50"] - 4.0) < 0.01  # mock jitter cancels out


def test_breaker_stats_from_a_timeline() -> None:
    tl = (
        [
            {"t": t / 10, "provider": "bench/primary", "attempts": "1", "status": 200}
            for t in range(0, 100)
        ]
        + [
            {"t": 10.0, "provider": "bench/backup", "attempts": "3", "status": 200},
            {"t": 10.1, "provider": "bench/backup", "attempts": "3", "status": 200},
            {"t": 10.2, "provider": "bench/backup", "attempts": "2", "status": 200},
        ]
        + [
            {"t": 10.3 + t / 10, "provider": "bench/backup", "attempts": "1", "status": 200}
            for t in range(150)
        ]
        + [{"t": 31.0, "provider": "bench/primary", "attempts": "1", "status": 200}]
    )
    s = run_bench.breaker_stats(tl, fail_at=10.0, ok_at=25.0)
    assert s["requests_that_paid"] == 3 and s["failed_attempts"] == 5
    assert s["recover_s"] == 6.0 and s["client_errors"] == 0
