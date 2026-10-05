"""Phase 3 DoD on the running stack (real Redis breaker, fake provider). Free.

`chaos-down` = [fake/down, fake/ok]. Every request must succeed; once the breaker in
Redis opens, requests skip the dead primary (1 attempt instead of 3).
"""

import concurrent.futures

import httpx
import pytest

GATEWAY = "http://localhost:8000"
pytestmark = pytest.mark.e2e


def test_primary_down_all_requests_succeed_via_fallback(gateway_key: str) -> None:
    def call(_: int) -> httpx.Response:
        return httpx.post(
            f"{GATEWAY}/v1/chat/completions",
            timeout=30,
            headers={"Authorization": f"Bearer {gateway_key}"},
            json={"model": "chaos-down", "messages": [{"role": "user", "content": "x"}]},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        responses = list(ex.map(call, range(100)))
    assert [r.status_code for r in responses].count(200) == 100
    assert all(r.headers["x-gateway-provider"] == "fake/ok" for r in responses)
    # breaker (in Redis) opened: later requests went straight to the fallback
    assert call(0).headers["x-gateway-attempts"] == "1"
