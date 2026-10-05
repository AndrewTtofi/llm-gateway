"""Policy routing: build a fallback chain per request from the catalog (ADR 0017).

An alias can have a `policy` instead of a fixed `chain`:

    aliases:
      auto:
        policy: { optimize: cost, min_quality: 3, max_chain: 4 }

For each request the candidates (the policy's list, or every known model) are filtered:

- usable: the provider has its key; the breaker isn't open;
- capable: has every capability needed, both the policy's `needs` and what the request
  implies (tools → tools, image parts → vision, a JSON schema → json_schema);
- fits: context window ≥ the estimated prompt + max_tokens;
- `min_quality` / `max_blended_price` when set (unknown values don't qualify).

Then they're ranked (`cost`: cheapest blended price; `quality`: best score, then cheapest;
`latency`: fastest measured time to first token, then cheapest) and the first `max_chain`
become this request's chain. Retries, fallback and breakers work as for any alias.

Clients may tighten a policy, never widen it, with a `route` object in the body or an
`x-gateway-route` header (`optimize=quality; needs=tools,vision; min_quality=4;
max_price=5`): a different `optimize`, more `needs`, a higher `min_quality`, a lower
`max_blended_price`. The candidate list is the operator's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import config, providers
from app.config import Capability, CatalogEntry, Policy
from app.observability import live
from app.providers.openai_compat import rules_for
from app.routing import router
from app.routing.breaker import State

BLEND_INPUT, BLEND_OUTPUT = 3, 1  # the same blend /v1/catalog shows
CAPABILITIES: tuple[Capability, ...] = ("tools", "vision", "reasoning", "json_schema")


class NoRoute(Exception):
    """No candidate satisfies the policy. `capacity`: they exist but are unavailable now
    (breakers open, keys missing) → 503; otherwise the constraints exclude all → 400."""

    def __init__(self, message: str, capacity: bool) -> None:
        super().__init__(message)
        self.capacity = capacity


@dataclass
class Plan:
    chain: list[str]
    optimize: str
    considered: int
    excluded: dict[str, int] = field(default_factory=dict)  # reason → count

    def header(self) -> str:
        return f"optimize={self.optimize}; considered={self.considered}; chain={len(self.chain)}"


def usable_capabilities(caps: list[str], target: str) -> list[str]:
    """What the gateway can use: an OpenAI-compatible model with `tools: false` /
    `vision: false` loses those even if the model itself has them (ADR 0012)."""
    provider, _, model = target.partition("/")
    cfg = config.registry.providers.get(provider) or {}
    if cfg.get("type") != "openai":
        return list(caps)
    rules = rules_for(cfg, model)
    return [
        c
        for c in caps
        if not (c == "tools" and not rules["tools"]) and not (c == "vision" and not rules["vision"])
    ]


def blended(target: str) -> float | None:
    price = config.pricing.models.get(target)
    if price is None or price.input is None or price.output is None:
        return None
    return (BLEND_INPUT * price.input + BLEND_OUTPUT * price.output) / (BLEND_INPUT + BLEND_OUTPUT)


def implied_needs(body: dict[str, Any]) -> set[str]:
    needs: set[str] = set()
    if body.get("tools") or body.get("functions"):
        needs.add("tools")
    for msg in body.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            needs.add("vision")
    if (body.get("response_format") or {}).get("type") == "json_schema":
        needs.add("json_schema")
    return needs


def parse_header(value: str) -> dict[str, Any]:
    """`optimize=cost; needs=tools,vision; min_quality=3; max_price=2.5` → hints."""
    hints: dict[str, Any] = {}
    for part in value.split(";"):
        key, _, raw = part.strip().partition("=")
        key, raw = key.strip().lower(), raw.strip()
        if not key or not raw:
            continue
        if key == "needs":
            hints["needs"] = [n.strip() for n in raw.split(",") if n.strip()]
        elif key in ("max_price", "max_blended_price"):
            hints["max_blended_price"] = raw
        else:
            hints[key] = raw
    return hints


def effective(policy: Policy, hints: dict[str, Any] | None) -> Policy:
    """Apply client hints, which can only tighten. Invalid hints are a ValueError (400)."""
    if not hints or not policy.client_hints:
        return policy
    allowed = {"optimize", "needs", "min_quality", "max_blended_price"}
    if unknown := set(hints) - allowed:
        raise ValueError(f"unknown route hints: {sorted(unknown)}; allowed: {sorted(allowed)}")
    merged = policy.model_dump()
    if "optimize" in hints:
        merged["optimize"] = hints["optimize"]
    if "needs" in hints:
        needs = hints["needs"]
        if not isinstance(needs, list):
            raise ValueError("route.needs must be a list")
        merged["needs"] = sorted(set(merged["needs"]) | {str(n) for n in needs})
    if "min_quality" in hints:
        q = int(hints["min_quality"])
        merged["min_quality"] = max(q, merged["min_quality"] or q)
    if "max_blended_price" in hints:
        cap = float(hints["max_blended_price"])
        current = merged["max_blended_price"]
        merged["max_blended_price"] = cap if current is None else min(cap, current)
    return Policy.model_validate(merged)  # validates optimize / needs / ranges


def candidates(alias: str, policy: Policy) -> list[str]:
    reg = config.registry
    if policy.candidates:
        return list(policy.candidates)
    test_providers = {
        n for n, p in reg.providers.items() if p.get("type") == "fake" or p.get("dev_only")
    }
    return sorted(t for t in reg.known_targets() if t.partition("/")[0] not in test_providers)


async def plan(alias: str, policy: Policy, body: dict[str, Any], prompt_tokens: int) -> Plan:
    """The chain for this request (see the module docstring)."""
    reg, catalog = config.registry, config.catalog.models
    needs = set(policy.needs) | implied_needs(body)
    max_out = int(body.get("max_completion_tokens") or body.get("max_tokens") or 0)
    context_needed = prompt_tokens + max_out
    excluded: dict[str, int] = {}

    def skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    # 1. The constraints: capabilities, context, quality, price.
    pool = candidates(alias, policy)
    fitting: list[tuple[str, CatalogEntry]] = []
    for target in pool:
        if reg.providers.get(target.partition("/")[0]) is None:
            skip("unknown_provider")
            continue
        facts = catalog.get(target) or CatalogEntry()
        caps = set(usable_capabilities(list(facts.capabilities), target))
        if needs - caps:
            skip("capability")
            continue
        if facts.context_window is not None and facts.context_window < context_needed:
            skip("context")
            continue
        if policy.min_quality is not None and (facts.quality or 0) < policy.min_quality:
            skip("quality")
            continue
        price = blended(target)
        if policy.max_blended_price is not None and (
            price is None or price > policy.max_blended_price
        ):
            skip("price")
            continue
        fitting.append((target, facts))
    if not fitting:  # the request asks for something no candidate can do
        raise NoRoute(
            f"no model matches the routing policy for '{alias}' (excluded: {excluded})",
            capacity=False,
        )

    # 2. Availability right now: provider key present, breaker not open.
    ranked: list[tuple[str, CatalogEntry]] = []
    for target, facts in fitting:
        provider = target.partition("/")[0]
        try:
            configured = providers.pool.get(provider, reg.providers[provider]).configured
        except providers.UnsupportedProvider:
            configured = False
        if not configured:
            skip("not_configured")
            continue
        if await router.store.state(target) == State.OPEN:
            skip("circuit_open")
            continue
        ranked.append((target, facts))
    if not ranked:  # models would fit, but none can serve now: retryable
        raise NoRoute(
            f"no model for '{alias}' is available right now (excluded: {excluded})", capacity=True
        )

    big = float("inf")

    def by_price(t: str) -> float:
        p = blended(t)
        return p if p is not None else big

    def key(item: tuple[str, CatalogEntry]) -> tuple[Any, ...]:
        t, f = item
        if policy.optimize == "quality":
            return (-(f.quality or 0), by_price(t), t)
        if policy.optimize == "latency":
            snap = live.snapshot(t)
            stat = snap["ttft_ms"] or snap["latency_ms"]
            return (stat["p50"] if stat else big, by_price(t), t)
        return (by_price(t), -(f.quality or 0), t)

    ranked.sort(key=key)
    return Plan(
        chain=[t for t, _ in ranked[: policy.max_chain]],
        optimize=policy.optimize,
        considered=len(pool),
        excluded=excluded,
    )
