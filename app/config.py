"""Settings from env + model registry from config/*.yaml (hot-reloadable)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    gateway_admin_key: str = ""
    log_level: str = "INFO"
    config_dir: Path = Path("config")


class Alias(BaseModel):
    chain: list[str]  # ["provider/model", ...] in fallback order


class Registry(BaseModel):
    providers: dict[str, dict[str, Any]]
    aliases: dict[str, Alias]
    allow_direct_models: bool = True
    retry: dict[str, Any] = {}
    circuit_breaker: dict[str, Any] = {}

    def resolve(self, model: str) -> list[str]:
        """Return the fallback chain for an alias or a direct provider/model."""
        if model in self.aliases:
            return self.aliases[model].chain
        if self.allow_direct_models and "/" in model:
            return [model]
        raise KeyError(f"Unknown model or alias: {model}")


_ENV = re.compile(r"\$\{(\w+)\}")


def _expand_env(text: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), text)


def load_registry(config_dir: Path) -> Registry:
    raw = _expand_env((config_dir / "models.yaml").read_text())
    return Registry.model_validate(yaml.safe_load(raw))


settings = Settings()
registry = load_registry(settings.config_dir)


def reload_registry() -> Registry:
    global registry
    registry = load_registry(settings.config_dir)
    return registry
