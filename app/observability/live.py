"""Recent per-target performance, as this replica saw it, for GET /v1/catalog (ADR 0011).

Prometheus holds the real history; this is a small in-process window so apps can compare
models without querying Prometheus. Each replica reports its own traffic.

- Attempts (every upstream call, success or failure) give the error rate.
- Served requests give latency and time to first token, measured from the start of the
  attempt that served. A fallback isn't charged for the failed attempts before it.

Memory is bounded: targets come from config, never from client input, and each keeps at
most `MAX_SAMPLES` samples.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

WINDOW_SECONDS = 900.0
MAX_SAMPLES = 2000


@dataclass
class _Target:
    attempts: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES))
    served: deque[tuple[float, float, float | None]] = field(
        default_factory=lambda: deque(maxlen=MAX_SAMPLES)
    )  # (time, latency s, ttft s or None)


_targets: dict[str, _Target] = {}


def _clock() -> float:
    return time.monotonic()


def record_attempt(target: str, ok: bool) -> None:
    _targets.setdefault(target, _Target()).attempts.append((_clock(), ok))


def record_served(target: str, latency: float, ttft: float | None) -> None:
    _targets.setdefault(target, _Target()).served.append((_clock(), latency, ttft))


def reset() -> None:
    _targets.clear()


def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


def _ms(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"p50": round(_pct(values, 50) * 1000, 1), "p95": round(_pct(values, 95) * 1000, 1)}


def snapshot(target: str, window: float = WINDOW_SECONDS) -> dict[str, Any]:
    """Stats over the last `window` seconds; `requests: 0` and nulls when there's no data."""
    t = _targets.get(target)
    since = _clock() - window
    attempts = [ok for at, ok in (t.attempts if t else ()) if at >= since]
    served = [(lat, ttft) for at, lat, ttft in (t.served if t else ()) if at >= since]
    return {
        "window_seconds": int(window),
        "requests": len(served),
        "attempts": len(attempts),
        "error_rate": round(1 - sum(attempts) / len(attempts), 4) if attempts else None,
        "latency_ms": _ms([lat for lat, _ in served]),
        "ttft_ms": _ms([ttft for _, ttft in served if ttft is not None]),
    }
