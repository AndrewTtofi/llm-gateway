"""Phase 3 DoD on the running stack (real Redis breaker, fake provider). Free.

`chaos-down` = [fake/down, fake/ok]. Every request must succeed; once the breaker in
Redis opens, requests skip the dead primary (1 attempt instead of 3).
"""

import concurrent.futures

import httpx
import pytest

GATEWAY = "http://localhost:8000"
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def stack_is_up() -> None:
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("gateway not running (make up)")


def call(_: int) -> httpx.Response:
    return httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        timeout=30,
        json={"model": "chaos-down", "messages": [{"role": "user", "content": "x"}]},
    )


def test_primary_down_all_requests_succeed_via_fallback() -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        responses = list(ex.map(call, range(100)))
    assert [r.status_code for r in responses].count(200) == 100
    assert all(r.headers["x-gateway-provider"] == "fake/ok" for r in responses)
    # breaker (in Redis) opened: later requests went straight to the fallback
    assert call(0).headers["x-gateway-attempts"] == "1"
