"""Phase 4 DoD on the running stack (Redis buckets, Postgres keys, fake provider). Free.

- limits enforced within ±5% under load from many concurrent clients
- the budget cap blocks further calls
"""

import concurrent.futures
import time
from collections.abc import Callable

import httpx
import pytest

GATEWAY = "http://localhost:8000"
pytestmark = pytest.mark.e2e


def post(key: str, model: str) -> httpx.Response:
    return httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        timeout=30,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": "x"}]},
    )


def test_request_limit_enforced_within_5_percent(mint_key: Callable[..., str]) -> None:
    rpm, seconds = 120, 10.0
    key = mint_key(requests_per_minute=rpm, allowed_aliases=["fake/ok"])
    deadline = time.monotonic() + seconds

    def client(_: int) -> tuple[int, int]:
        ok = limited = 0
        while time.monotonic() < deadline:
            status = post(key, "fake/ok").status_code
            ok += status == 200
            limited += status == 429
        return ok, limited

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        results = list(ex.map(client, range(30)))
    allowed = sum(r[0] for r in results)
    limited = sum(r[1] for r in results)
    expected = rpm + rpm / 60 * seconds  # a full minute's burst + refill
    print(f"\nallowed={allowed} expected={expected:.0f} rate-limited={limited}")
    assert limited > allowed  # the clients pushed well past the limit
    assert abs(allowed - expected) / expected <= 0.05


def test_budget_cap_blocks_further_calls(
    mint_key: Callable[..., str], admin_headers: dict[str, str]
) -> None:
    budget = 0.05  # fake/metered costs $0.001/token, a few tokens per call
    key = mint_key(monthly_budget_usd=budget, allowed_aliases=["fake/metered"])
    statuses = [post(key, "fake/metered").status_code for _ in range(30)]
    print(f"\nstatuses: {statuses}")
    assert statuses[0] == 200
    first_block = statuses.index(429)
    assert all(s == 429 for s in statuses[first_block:])  # stays blocked
    assert post(key, "fake/metered").json()["error"]["code"] == "insufficient_quota"

    keys = httpx.get(f"{GATEWAY}/admin/keys", headers=admin_headers, timeout=10).json()["data"]
    spent = next(k["spent_this_month_usd"] for k in keys if k["prefix"] == key[:8])
    per_call = spent / first_block
    print(f"spent=${spent:.4f} budget=${budget} per-call=${per_call:.4f}")
    # blocked once the budget was used up, overshooting by less than one call
    assert budget <= spent < budget + per_call
