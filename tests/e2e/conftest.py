"""e2e fixtures: mint gateway keys on the running stack through the admin API."""

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import httpx
import pytest

GATEWAY = "http://localhost:8000"


@pytest.fixture(scope="session")
def admin_headers() -> dict[str, str]:
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("gateway not running (make up)")
    # Read the admin key from the container's environment (never printed or logged).
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not found")
    out = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [docker, "compose", "exec", "-T", "gateway", "printenv", "GATEWAY_ADMIN_KEY"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("GATEWAY_ADMIN_KEY not set in the gateway container")
    return {"Authorization": f"Bearer {out.stdout.strip()}"}


@pytest.fixture(scope="session")
def mint_key(admin_headers: dict[str, str]) -> Callable[..., str]:
    created: list[str] = []

    def mint(tier: str = "chaos", **overrides: Any) -> str:
        r = httpx.post(
            f"{GATEWAY}/admin/keys",
            headers=admin_headers,
            timeout=10,
            json={"name": "e2e", "tier": tier, **overrides},
        )
        r.raise_for_status()
        created.append(r.json()["id"])
        return str(r.json()["key"])

    yield mint  # type: ignore[misc]
    for kid in created:  # don't leave usable test keys behind
        httpx.delete(f"{GATEWAY}/admin/keys/{kid}", headers=admin_headers, timeout=10)


@pytest.fixture(scope="session")
def gateway_key(mint_key: Callable[..., str]) -> str:
    return mint_key(
        allowed_aliases=["*"],
        requests_per_minute=100000,
        tokens_per_minute=10**9,
        monthly_budget_usd=1000,
    )
