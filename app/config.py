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
    # external = Redis + Postgres (default). memory = everything in-process: tests, or a
    # quick single-process run without the stack. Keys and limits then die with the process.
    gateway_stores: Literal["external", "memory"] = "external"
    db_timeout_seconds: float = 2.0  # pool wait, connect and query timeout on the request path
    # Prometheus metrics on their own port (internal only). 0 = serve /metrics on the API port.
    metrics_port: int = 9100


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


class Estimation(BaseModel):
    chars_per_token: float = Field(default=4, gt=0)
    default_completion_tokens: int = Field(default=1024, ge=0)


class Tier(BaseModel):
    requests_per_minute: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)
    monthly_budget_usd: float = Field(ge=0)
    allowed_aliases: list[str]


class Limits(BaseModel):
    estimation: Estimation = Field(default_factory=Estimation)
    tiers: dict[str, Tier]


class Price(BaseModel):
    input: float | None = None  # USD per 1M tokens; None = unknown
    output: float | None = None
    cached_input: float | None = None  # cache reads; None = billed as normal input


class Pricing(BaseModel):
    currency: str = "USD"
    models: dict[str, Price] = {}

    def cost(
        self, target: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0
    ) -> float | None:
        """USD for one call, or None if the target has no (complete) price.

        `prompt_tokens` includes `cached_tokens` (OpenAI convention); cached ones are
        billed at `cached_input` when the model has one.
        """
        price = self.models.get(target)
        if price is None or price.input is None or price.output is None:
            return None
        cached = min(max(cached_tokens, 0), prompt_tokens)
        cached_rate = price.cached_input if price.cached_input is not None else price.input
        return (
            (prompt_tokens - cached) * price.input
            + cached * cached_rate
            + completion_tokens * price.output
        ) / 1_000_000


_ENV = re.compile(r"\$\{(\w+)\}")


def _expand_env(text: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), text)


def load_registry(config_dir: Path, enable_fake: bool = False) -> Registry:
    raw = yaml.safe_load(_expand_env((config_dir / "models.yaml").read_text()))
    if not enable_fake:
        _drop_fake(raw)
    return Registry.model_validate(raw)


def _drop_fake(raw: dict[str, Any]) -> None:
    """Remove test providers (type fake, or `dev_only: true`) and their chain entries;
    drop aliases left empty."""
    fake = {
        n
        for n, p in (raw.get("providers") or {}).items()
        if p.get("type") == "fake" or p.get("dev_only")
    }
    for name in fake:
        del raw["providers"][name]
    for alias in list(raw.get("aliases") or {}):
        chain = [t for t in raw["aliases"][alias]["chain"] if t.split("/", 1)[0] not in fake]
        if chain:
            raw["aliases"][alias]["chain"] = chain
        else:
            del raw["aliases"][alias]


def load_limits(config_dir: Path) -> Limits:
    return Limits.model_validate(yaml.safe_load((config_dir / "limits.yaml").read_text()))


def load_pricing(config_dir: Path) -> Pricing:
    return Pricing.model_validate(yaml.safe_load((config_dir / "pricing.yaml").read_text()))


settings = Settings()
registry = load_registry(settings.config_dir, settings.gateway_enable_fake)
limits = load_limits(settings.config_dir)
pricing = load_pricing(settings.config_dir)


def reload_registry() -> Registry:
    """Reload models, limits and pricing together. If any file is broken, raise and keep
    all three as they were — never a half-applied config."""
    global registry, limits, pricing
    new_registry = load_registry(settings.config_dir, settings.gateway_enable_fake)
    new_limits = load_limits(settings.config_dir)
    new_pricing = load_pricing(settings.config_dir)
    registry, limits, pricing = new_registry, new_limits, new_pricing
    return registry
