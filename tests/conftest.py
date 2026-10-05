from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.config import Registry
from app.providers import AdapterPool

UPSTREAM = "http://upstream.test/v1"


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
                    "timeouts": {"connect": 1, "first_token": 2, "total": 5},
                },
                "notyet": {"type": "carrier-pigeon"},
            },
            "aliases": {
                "local": {"chain": ["mock/tiny"]},
                "unsupported": {"chain": ["notyet/bird"]},
            },
        }
    )
    monkeypatch.setattr(config, "registry", reg)
    monkeypatch.setattr(main, "pool", AdapterPool())  # fresh HTTP clients per test
    return reg


@pytest.fixture
def client(registry: Registry) -> Iterator[TestClient]:
    with TestClient(main.app) as c:
        yield c
