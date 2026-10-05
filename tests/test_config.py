from pathlib import Path

import pytest
import yaml

from app.config import load_registry


def test_aliases_resolve_to_chains() -> None:
    reg = load_registry(Path("config"))
    assert len(reg.resolve("smart")) >= 1
    assert all("/" in m for m in reg.resolve("fast"))


def test_direct_model_allowed() -> None:
    reg = load_registry(Path("config"))
    assert reg.resolve("ollama/llama3.2:3b") == ["ollama/llama3.2:3b"]


def test_unknown_alias_raises() -> None:
    reg = load_registry(Path("config"))
    with pytest.raises(KeyError):
        reg.resolve("does-not-exist")


def test_every_chain_model_has_a_known_provider() -> None:
    reg = load_registry(Path("config"))
    for alias in reg.aliases.values():
        for entry in alias.chain:
            assert entry.split("/", 1)[0] in reg.providers, entry


def test_every_chain_model_has_a_price_entry() -> None:
    reg = load_registry(Path("config"))
    prices = yaml.safe_load(Path("config/pricing.yaml").read_text())["models"]
    for alias in reg.aliases.values():
        for entry in alias.chain:
            assert entry in prices, f"{entry} missing from pricing.yaml"
