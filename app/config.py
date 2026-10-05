"""Settings from env + model registry from config/*.yaml (hot-reloadable)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    gateway_admin_key: str = ""
    log_level: str = "INFO"
    config_dir: Path = Path("config")
    # The chaos provider (type: fake) and every alias using it only load when this is
    # set — the dev compose stack sets it; production must not.
    gateway_enable_fake: bool = False


class Alias(BaseModel):
    chain: list[str]  # ["provider/model", ...] in fallback order


class RetryConfig(BaseModel):
    max_attempts_per_provider: int = Field(default=2, ge=1, le=10)
    backoff_base_ms: int = Field(default=250, ge=0)
    backoff_max_ms: int = Field(default=4000, ge=0)
    retry_on_status: list[int] = [408, 409, 429, 500, 502, 503, 504, 529]


class BreakerConfig(BaseModel):
    store: Literal["redis", "memory"] = "redis"
    failure_threshold: int = Field(default=5, ge=1)
    window_seconds: float = Field(default=60, gt=0)
    open_seconds: float = Field(default=30, gt=0)
    probe_timeout_seconds: float = Field(default=330, gt=0)
    redis_timeout_ms: int = Field(default=100, ge=10)


class Registry(BaseModel):
    providers: dict[str, dict[str, Any]]
    aliases: dict[str, Alias]
    allow_direct_models: bool = True
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: BreakerConfig = Field(default_factory=BreakerConfig)

    def known_targets(self) -> set[str]:
        """provider/model names the gateway knows: alias chains + models listed per provider."""
        known = {t for a in self.aliases.values() for t in a.chain}
        for name, cfg in self.providers.items():
            known |= {f"{name}/{m}" for m in cfg.get("models", {})}
        return known

    def resolve(self, model: str) -> list[str]:
        """Return the fallback chain for an alias or a direct provider/model.

        Direct names must be known: arbitrary ones would each get their own circuit
        breaker (unbounded state) and a 404 upstream.
        """
        if model in self.aliases:
            return self.aliases[model].chain
        if self.allow_direct_models and model in self.known_targets():
            return [model]
        raise KeyError(f"Unknown model or alias: {model}")


_ENV = re.compile(r"\$\{(\w+)\}")


def _expand_env(text: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), text)


def load_registry(config_dir: Path, enable_fake: bool = False) -> Registry:
    raw = yaml.safe_load(_expand_env((config_dir / "models.yaml").read_text()))
    if not enable_fake:
        _drop_fake(raw)
    return Registry.model_validate(raw)


def _drop_fake(raw: dict[str, Any]) -> None:
    """Remove fake providers and their chain entries; drop aliases left empty."""
    fake = {n for n, p in (raw.get("providers") or {}).items() if p.get("type") == "fake"}
    for name in fake:
        del raw["providers"][name]
    for alias in list(raw.get("aliases") or {}):
        chain = [t for t in raw["aliases"][alias]["chain"] if t.split("/", 1)[0] not in fake]
        if chain:
            raw["aliases"][alias]["chain"] = chain
        else:
            del raw["aliases"][alias]


settings = Settings()
registry = load_registry(settings.config_dir, settings.gateway_enable_fake)


def reload_registry() -> Registry:
    """Swap in a freshly loaded registry. On a broken file, raise and keep the old one."""
    global registry
    new = load_registry(settings.config_dir, settings.gateway_enable_fake)  # raises first
    registry = new
    return registry
