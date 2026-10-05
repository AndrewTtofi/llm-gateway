"""Settings from env + model registry from config/*.yaml (hot-reloadable)."""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
    # Largest request body accepted (413 above it). Base64 images make bodies big:
    # Anthropic allows up to 32 MB per request.
    max_body_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    # Prometheus metrics on their own port (internal only). 0 = serve /metrics on the API port.
    metrics_port: int = 9100


class Policy(BaseModel):
    """Build the chain per request from the catalog instead of a fixed list (ADR 0017)."""

    model_config = ConfigDict(extra="forbid")

    optimize: Literal["cost", "quality", "latency"] = "cost"
    candidates: list[str] = []  # targets to choose from; empty = every known model
    needs: list[Literal["tools", "vision", "reasoning", "json_schema"]] = []
    min_quality: int | None = Field(default=None, ge=1, le=5)
    max_blended_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_chain: int = Field(default=4, ge=1, le=10)
    client_hints: bool = True  # clients may tighten it (route field / x-gateway-route)


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["exact", "semantic"] = "exact"
    ttl_seconds: int = Field(default=3600, ge=1, le=30 * 86400)
    scope: Literal["key", "team", "global"] = "key"
    threshold: float = Field(default=0.95, gt=0, le=1)  # semantic only
    embedding: str | None = None  # provider/model (OpenAI-compatible /embeddings), semantic only
    max_entries: int = Field(default=10_000, ge=1)  # per scope and alias (semantic index)
    max_entry_bytes: int = Field(default=256 * 1024, ge=1024)


class Variant(BaseModel):
    """One arm of an A/B test (ADR 0020)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")  # a metric label: kept bounded
    weight: float = Field(gt=0)
    chain: list[str] = Field(min_length=1)
    # Prepended to the system prompt for this arm (prompt A/B tests).
    system_prefix: str | None = Field(default=None, max_length=4000)


class Alias(BaseModel):
    chain: list[str] = []  # ["provider/model", ...] in fallback order
    policy: Policy | None = None  # instead of a chain: chosen per request
    variants: list[Variant] = []  # instead of a chain: an A/B test (ADR 0020)
    sticky: Literal["key", "user", "request"] = "key"  # what keeps a caller on one variant
    cache: CacheConfig | None = None  # response cache, opt-in (ADR 0018)

    @model_validator(mode="after")
    def _one_of(self) -> Alias:
        if sum((bool(self.chain), self.policy is not None, bool(self.variants))) != 1:
            raise ValueError("an alias needs exactly one of `chain`, `policy` or `variants`")
        names = [v.name for v in self.variants]
        if len(names) != len(set(names)):
            raise ValueError("variant names must be unique")
        return self

    @property
    def targets(self) -> list[str]:
        """Every target this alias can route to (for listings, pricing checks, probes)."""
        return list(dict.fromkeys([*self.chain, *(t for v in self.variants for t in v.chain)]))


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


class SelfHealing(BaseModel):
    """Background recovery and alerting (ADR 0019)."""

    model_config = ConfigDict(extra="forbid")

    probes: bool = True  # probe half-open targets in the background, not with user traffic
    probe_interval_seconds: float = Field(default=10, gt=0)
    probe_max_tokens: int = Field(default=16, ge=1, le=64)  # Responses API minimum is 16
    # Faults that won't heal in seconds: hold the breaker open longer.
    quarantine_seconds: float = Field(default=600, gt=0)
    quarantine_status: list[int] = [401, 403, 404]
    alert_webhook_env: str | None = "ALERT_WEBHOOK_URL"  # env var holding the URL; unset = off
    alert_min_interval_seconds: float = Field(default=60, ge=0)  # per target and state, fleet-wide


class Registry(BaseModel):
    providers: dict[str, dict[str, Any]]
    aliases: dict[str, Alias]
    allow_direct_models: bool = True
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    self_healing: SelfHealing = Field(default_factory=SelfHealing)

    def known_targets(self) -> set[str]:
        """provider/model names the gateway knows: alias chains + models listed per provider."""
        known = {t for a in self.aliases.values() for t in a.targets}
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


class Team(BaseModel):
    monthly_budget_usd: float = Field(ge=0)


class Limits(BaseModel):
    estimation: Estimation = Field(default_factory=Estimation)
    tiers: dict[str, Tier]
    teams: dict[str, Team] = {}  # keys with `team` also count against the team's budget


# A negative or non-finite price would corrupt spend and let keys past their budgets.
_PRICE = Field(default=None, ge=0, allow_inf_nan=False)


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class PriceTier(BaseModel):
    """Prices once a request's prompt is above a size (ADR 0015). Providers that tier
    (OpenAI above 272K, Gemini and xAI above 200K) reprice the *whole* request."""

    above_prompt_tokens: int = Field(gt=0)
    input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)
    cached_input: float | None = _PRICE
    cache_write: float | None = _PRICE
    cache_write_1h: float | None = _PRICE


class OffPeak(BaseModel):
    """Time-of-day pricing (DeepSeek): outside the peak windows, every rate is multiplied."""

    multiplier: float = Field(gt=0, le=1)
    peak_utc: list[str] = Field(min_length=1)  # "HH:MM-HH:MM", end exclusive
    peak_days: list[Weekday] = list(WEEKDAYS[:5])

    @field_validator("peak_utc")
    @classmethod
    def _windows(cls, value: list[str]) -> list[str]:
        for window in value:
            m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-(([01]\d|2[0-3]):([0-5]\d)|24:00)", window)
            if not m:
                raise ValueError(f"peak window {window!r} is not HH:MM-HH:MM (end may be 24:00)")
            start, end = (int(t[:2]) * 60 + int(t[3:]) for t in window.split("-"))
            if start >= end:  # a window crossing midnight is two windows: 22:00-24:00, 00:00-02:00
                raise ValueError(f"peak window {window!r} must end after it starts")
        return value

    def is_peak(self, at: datetime) -> bool:
        at = at.astimezone(UTC)
        if WEEKDAYS[at.weekday()] not in self.peak_days:
            return False
        minute = at.hour * 60 + at.minute
        for window in self.peak_utc:
            start, end = (
                int(h) * 60 + int(m) for h, m in (t.split(":") for t in window.split("-"))
            )
            if start <= minute < end:
                return True
        return False


class Price(BaseModel):
    input: float | None = _PRICE  # USD per 1M tokens; None = unknown
    output: float | None = _PRICE
    cached_input: float | None = _PRICE  # cache reads; None = billed as normal input
    cache_write: float | None = _PRICE  # prompt-cache writes (5-minute); None = input price
    cache_write_1h: float | None = _PRICE  # 1-hour cache writes; None = cache_write
    tiers: list[PriceTier] = []
    off_peak: OffPeak | None = None

    @field_validator("tiers")
    @classmethod
    def _ascending(cls, tiers: list[PriceTier]) -> list[PriceTier]:
        thresholds = [t.above_prompt_tokens for t in tiers]
        if thresholds != sorted(set(thresholds)):
            raise ValueError("tiers must have distinct, ascending above_prompt_tokens")
        return tiers


class Pricing(BaseModel):
    checked: date | None = None  # when prices were last compared with public catalogs
    currency: str = "USD"
    models: dict[str, Price] = {}

    def cost(
        self,
        target: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        written_tokens: int = 0,
        written_1h_tokens: int = 0,
        at: datetime | None = None,
    ) -> float | None:
        """USD for one call, or None if the target has no (complete) price.

        `prompt_tokens` includes cache reads and writes (OpenAI convention). Reads are
        billed at `cached_input`, writes at `cache_write` / `cache_write_1h`, and the rest
        at `input`. The tier is chosen by the prompt size; off-peak pricing by `at` (UTC,
        default now).
        """
        price = self.models.get(target)
        if price is None or price.input is None or price.output is None:
            return None
        rates: Price | PriceTier = price
        for tier in price.tiers:  # ascending: the last one the prompt exceeds wins
            if prompt_tokens > tier.above_prompt_tokens:
                rates = tier
        assert rates.input is not None and rates.output is not None
        cached = min(max(cached_tokens, 0), prompt_tokens)
        written = min(max(written_tokens, 0), prompt_tokens - cached)
        written_1h = min(max(written_1h_tokens, 0), written)
        read_rate = rates.cached_input if rates.cached_input is not None else rates.input
        write_rate = rates.cache_write if rates.cache_write is not None else rates.input
        write_1h_rate = rates.cache_write_1h if rates.cache_write_1h is not None else write_rate
        usd = (
            (prompt_tokens - cached - written) * rates.input
            + cached * read_rate
            + (written - written_1h) * write_rate
            + written_1h * write_1h_rate
            + completion_tokens * rates.output
        ) / 1_000_000
        if price.off_peak is not None and not price.off_peak.is_peak(at or datetime.now(UTC)):
            usd *= price.off_peak.multiplier
        return usd


Capability = Literal["tools", "vision", "reasoning", "json_schema"]


class CatalogEntry(BaseModel):
    """Facts about one target, for /v1/catalog (ADR 0011). Unknown = None."""

    model_config = ConfigDict(extra="ignore")  # source-ID hints etc. are for the sync tool

    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    capabilities: list[Capability] = []
    quality: int | None = Field(default=None, ge=1, le=5)  # the operator's score

    def model_dump_public(self) -> dict[str, Any]:
        return self.model_dump(
            include={"context_window", "max_output_tokens", "capabilities", "quality"}
        )


class Catalog(BaseModel):
    checked: date | None = None  # when the synced facts were last checked
    models: dict[str, CatalogEntry] = {}


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
        spec = raw["aliases"][alias]
        if "policy" in spec:  # policy aliases: drop test providers from their candidates
            policy = spec["policy"] or {}
            if policy.get("candidates"):
                policy["candidates"] = [
                    t for t in policy["candidates"] if t.split("/", 1)[0] not in fake
                ]
                if not policy["candidates"]:
                    del raw["aliases"][alias]
            continue
        if spec.get("variants"):  # A/B aliases: drop test providers from each arm
            for v in spec["variants"]:
                v["chain"] = [t for t in v.get("chain") or [] if t.split("/", 1)[0] not in fake]
            spec["variants"] = [v for v in spec["variants"] if v["chain"]]
            if not spec["variants"]:
                del raw["aliases"][alias]
            continue
        chain = [t for t in spec.get("chain") or [] if t.split("/", 1)[0] not in fake]
        if chain:
            spec["chain"] = chain
        else:
            del raw["aliases"][alias]


def load_limits(config_dir: Path) -> Limits:
    return Limits.model_validate(yaml.safe_load((config_dir / "limits.yaml").read_text()))


def load_pricing(config_dir: Path) -> Pricing:
    return Pricing.model_validate(yaml.safe_load((config_dir / "pricing.yaml").read_text()))


def load_catalog(config_dir: Path) -> Catalog:
    path = config_dir / "catalog.yaml"  # optional: without it the catalog shows prices only
    return (
        Catalog.model_validate(yaml.safe_load(path.read_text()) or {})
        if path.exists()
        else Catalog()
    )


settings = Settings()
registry = load_registry(settings.config_dir, settings.gateway_enable_fake)
limits = load_limits(settings.config_dir)
pricing = load_pricing(settings.config_dir)
catalog = load_catalog(settings.config_dir)


def reload_registry() -> Registry:
    """Reload models, limits, pricing and the catalog together. If any file is broken,
    raise and keep all of them as they were — never a half-applied config."""
    global registry, limits, pricing, catalog
    new_registry = load_registry(settings.config_dir, settings.gateway_enable_fake)
    new_limits = load_limits(settings.config_dir)
    new_pricing = load_pricing(settings.config_dir)
    new_catalog = load_catalog(settings.config_dir)
    registry, limits, pricing, catalog = new_registry, new_limits, new_pricing, new_catalog
    return registry
