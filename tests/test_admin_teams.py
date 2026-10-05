"""PATCH /admin/keys, teams with budgets, and the request body limit."""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import config, main, services
from app.config import Limits, Registry, Team
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION

ADMIN = {"Authorization": "Bearer gw_admin_test"}
MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> TestClient:
    monkeypatch.setattr(config.settings, "gateway_admin_key", "gw_admin_test")
    teams = {"web": Team(monthly_budget_usd=1.0), "batch": Team(monthly_budget_usd=0.0)}
    monkeypatch.setattr(config, "limits", Limits(**{**config.limits.model_dump(), "teams": teams}))
    return TestClient(main.app)


def create(c: TestClient, **body: Any) -> dict[str, Any]:
    resp = c.post("/admin/keys", headers=ADMIN, json={"name": "app", "tier": "dev", **body})
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


def test_patch_edits_a_key_in_place(admin: TestClient) -> None:
    with admin as c:
        key = create(c, tokens_per_minute=1000)
        resp = c.patch(
            f"/admin/keys/{key['id']}",
            headers=ADMIN,
            json={
                "tier": "standard",
                "tokens_per_minute": None,
                "team": "web",
                "requests_per_minute": 7,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tier"] == "standard" and body["team"] == "web"
        assert body["overrides"] == {"requests_per_minute": 7}
        # the edit applies to the next request with the same plaintext key at once
        lim = c.get("/v1/models", headers={"Authorization": f"Bearer {key['key']}"})
        assert lim.status_code == 200


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"tier": "nope"}, 400),
        ({"team": "nope"}, 400),
        ({"name": None}, 400),
        ({"unknown_field": 1}, 400),
        ({"tokens_per_minute": 0}, 400),
    ],
)
def test_patch_rejects_bad_changes(admin: TestClient, change: dict[str, Any], status: int) -> None:
    with admin as c:
        key = create(c)
        assert c.patch(f"/admin/keys/{key['id']}", headers=ADMIN, json=change).status_code == status


def test_patch_needs_admin_and_an_active_key(admin: TestClient) -> None:
    with admin as c:
        key = create(c)
        assert c.patch(f"/admin/keys/{key['id']}", json={"name": "x"}).status_code == 401
        assert c.patch("/admin/keys/missing", headers=ADMIN, json={"name": "x"}).status_code == 404


def test_unknown_team_on_create_is_rejected(admin: TestClient) -> None:
    with admin as c:
        resp = c.post(
            "/admin/keys", headers=ADMIN, json={"name": "a", "tier": "dev", "team": "nope"}
        )
        assert resp.status_code == 400 and resp.json()["error"]["param"] == "team"


@respx.mock
def test_team_budget_blocks_every_key_in_the_team(admin: TestClient) -> None:
    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    with admin as c:
        broke = create(c, team="batch")  # team budget $0
        resp = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {broke['key']}"},
            json={"model": "local", "messages": MSGS},
        )
        assert resp.status_code == 429 and resp.json()["error"]["code"] == "insufficient_quota"
        assert "team" in resp.json()["error"]["message"]
        ok = create(c, team="web")
        resp = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ok['key']}"},
            json={"model": "local", "messages": MSGS},
        )
        assert resp.status_code == 200


async def test_team_spend_is_tracked_and_listed(admin: TestClient) -> None:
    with admin as c:
        key = create(c, team="web")
        await services.spend.add("team:web", 0.25)
        teams = {t["team"]: t for t in c.get("/admin/teams", headers=ADMIN).json()["data"]}
        assert teams["web"] == {
            "team": "web",
            "monthly_budget_usd": 1.0,
            "spent_this_month_usd": 0.25,
            "keys": 1,
        }
        assert teams["batch"]["keys"] == 0
        assert key["team"] == "web"


def test_meter_charges_the_team_too(admin: TestClient) -> None:
    import asyncio

    from app.auth import ApiKey
    from app.metering import Meter

    key = ApiKey(id="k1", name="n", prefix="gw_x", tier="dev", team="web")
    meter = Meter(key, key.limits(config.limits), services.limiter, services.spend, 100, 50, False)

    async def run() -> tuple[float, float]:
        meter.reserved_usd = 0.0
        await meter._add_spend(0.5)
        return await services.spend.spent("k1"), await services.spend.spent("team:web")

    assert asyncio.run(run()) == (0.5, 0.5)


# --- body size limit ----------------------------------------------------------------


def test_oversized_bodies_get_a_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "max_body_bytes", 200)
    big = {"model": "local", "messages": [{"role": "user", "content": "x" * 500}]}
    resp = client.post("/v1/chat/completions", json=big)
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "request_too_large"
    assert "x-request-id" in resp.headers
    resp = client.post("/v1/messages", json={**big, "max_tokens": 5})
    assert resp.status_code == 413 and resp.json()["type"] == "error"


@respx.mock
def test_chunked_bodies_are_limited_and_replayed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    monkeypatch.setattr(config.settings, "max_body_bytes", 400)
    small = json.dumps({"model": "local", "messages": MSGS}).encode()

    def chunks(data: bytes) -> Any:
        yield data[:10]
        yield data[10:]

    ok = client.post(
        "/v1/chat/completions", content=chunks(small), headers={"content-type": "application/json"}
    )
    assert ok.status_code == 200  # read in pieces, replayed whole
    big = json.dumps(
        {"model": "local", "messages": [{"role": "user", "content": "x" * 500}]}
    ).encode()
    resp = client.post(
        "/v1/chat/completions", content=chunks(big), headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
