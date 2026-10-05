from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.config import Registry
from app.providers import AdapterPool

UPSTREAM = "http://upstream.test/v1"
ANTHROPIC_UPSTREAM = "http://anthropic.test"


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
            "aliases": {
                "local": {"chain": ["mock/tiny"]},
                "unsupported": {"chain": ["notyet/bird"]},
                "claude-old": {"chain": ["claude/old"]},
                "claude-new": {"chain": ["claude/new"]},
            },
        }
    )
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-test")
    monkeypatch.setattr(config, "registry", reg)
    monkeypatch.setattr(main, "pool", AdapterPool())  # fresh HTTP clients per test
    return reg


@pytest.fixture
def client(registry: Registry) -> Iterator[TestClient]:
    with TestClient(main.app) as c:
        yield c
