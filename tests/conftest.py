from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import config, main, providers, services
from app.auth import ApiKey, CachedKeys, MemoryKeyStore, generate_key, hash_key
from app.config import Registry
from app.observability.usage import MemoryUsageSink
from app.providers import AdapterPool
from app.ratelimit import MemoryLimiter, MemorySpend
from app.routing import router
from app.routing.breaker import MemoryBreakerStore

UPSTREAM = "http://upstream.test/v1"
ANTHROPIC_UPSTREAM = "http://anthropic.test"


UNLIMITED = {
    "requests_per_minute": 10**9,
    "tokens_per_minute": 10**12,
    "monthly_budget_usd": 10**9,
    "allowed_aliases": ["*"],
}


def add_key(tier: str = "dev", **overrides: object) -> str:
    """Create a key straight in the in-memory store; returns the plaintext."""
    plaintext = generate_key()
    store = services.keys.store
    assert isinstance(store, MemoryKeyStore)
    store._by_hash[hash_key(plaintext)] = ApiKey(
        id=f"key-{len(store._by_hash)}",
        name="test",
        prefix=plaintext[:8],
        tier=tier,
        overrides=dict(overrides),
    )
    return plaintext


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Every test: in-memory stores (never the real Redis/Postgres) and one unlimited key."""
    monkeypatch.setattr(config.settings, "gateway_stores", "memory")
    monkeypatch.setattr(config.settings, "metrics_port", 0)  # /metrics on the app, no port
    monkeypatch.setattr(services, "keys", CachedKeys(MemoryKeyStore()))
    monkeypatch.setattr(services, "limiter", MemoryLimiter())
    monkeypatch.setattr(services, "spend", MemorySpend())
    monkeypatch.setattr(services, "usage", MemoryUsageSink())
    return add_key(**UNLIMITED)


@pytest.fixture
def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """A test-only registry: one mock OpenAI-compatible provider, no real models."""
    reg = Registry.model_validate(
        {
            "providers": {
                "mock": {
                    "type": "openai",
                    "base_url": UPSTREAM,
                    "api_key_env": "MOCK_API_KEY",
                    "timeouts": {"connect": 1, "first_token": 0.5, "stream_idle": 2, "total": 5},
                },
                "notyet": {"type": "carrier-pigeon"},
                "chaos": {
                    "type": "fake",
                    "timeouts": {"stream_total": 0.3},
                    "models": {
                        "ok": {},
                        "down": {"failure_rate": 1.0, "fail_status": [503]},
                        "unauthorized": {"failure_rate": 1.0, "fail_status": [401]},
                        "broken-stream": {"mid_stream_failure_rate": 1.0},
                        "trickle": {"chunk_delay_ms": 200},
                        "slow": {"latency_ms": [300, 300]},
                    },
                },
                "claude": {
                    "type": "anthropic",
                    "base_url": ANTHROPIC_UPSTREAM,
                    "api_key_env": "TEST_ANTHROPIC_KEY",
                    "timeouts": {"connect": 1, "first_token": 0.5, "stream_idle": 2, "total": 5},
                    "default_max_tokens": 1000,
                    "defaults": {"sampling": True, "forced_tool_choice": True, "effort": False},
                    "models": {
                        "new": {
                            "sampling": False,
                            "forced_tool_choice": False,
                            "effort": True,
                            "refusal_fallback": True,
                        },
                    },
                },
            },
            # never touch a real Redis in unit tests
            "circuit_breaker": {"store": "memory", "failure_threshold": 3, "open_seconds": 30},
            "retry": {"max_attempts_per_provider": 2, "backoff_base_ms": 1, "backoff_max_ms": 10},
            "aliases": {
                "local": {"chain": ["mock/tiny"]},
                "unsupported": {"chain": ["notyet/bird"]},
                "claude-old": {"chain": ["claude/old"]},
                "claude-new": {"chain": ["claude/new"]},
                "down-then-ok": {"chain": ["chaos/down", "chaos/ok"]},
                "only-down": {"chain": ["chaos/down"]},
                "auth-then-ok": {"chain": ["chaos/unauthorized", "chaos/ok"]},
                "mock-then-ok": {"chain": ["mock/tiny", "chaos/ok"]},
                "claude-then-mock": {"chain": ["claude/old", "mock/tiny"]},
                "broken": {"chain": ["chaos/broken-stream", "chaos/ok"]},
                "trickle": {"chain": ["chaos/trickle"]},
            },
        }
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-test")
    monkeypatch.setenv("MOCK_API_KEY", "mock-test-key")
    monkeypatch.setattr(config, "registry", reg)
    monkeypatch.setattr(providers, "pool", AdapterPool())  # fresh HTTP clients per test
    monkeypatch.setattr(router, "store", MemoryBreakerStore())  # fresh breakers per test
    return reg


@pytest.fixture
def client(registry: Registry, auth: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(main.app, headers=auth) as c:
        yield c
