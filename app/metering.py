"""Per-request accounting (ADR 0007): estimate → reserve → reconcile.

At admission the token bucket is charged an estimate and the estimated cost is
*reserved* against the budget, so a burst of long requests can't all slip in under a
stale month-to-date figure. `settle()` runs exactly once when the request ends —
success, failure or disconnect — and replaces both with the real numbers.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app import config
from app.auth import ApiKey, EffectiveLimits
from app.ratelimit import Limiter, SpendTracker

log = logging.getLogger(__name__)
_unpriced_warned: set[str] = set()


def _cost(target: str, prompt: int, completion: int) -> float:
    cost = config.pricing.cost(target, prompt, completion)
    if cost is None:
        if target not in _unpriced_warned:
            _unpriced_warned.add(target)
            log.warning("no price for %s in pricing.yaml; counting it as $0", target)
        return 0.0
    return cost


class Meter:
    def __init__(
        self,
        key: ApiKey,
        limits: EffectiveLimits,
        limiter: Limiter,
        spend: SpendTracker,
        estimate: int,
        prompt_estimate: int,
        client_wants_usage: bool,
    ) -> None:
        self.key, self.limits, self.limiter, self.spend = key, limits, limiter, spend
        self.estimate, self.prompt_estimate = estimate, prompt_estimate
        self.client_wants_usage = client_wants_usage
        self.usage: dict[str, Any] | None = None
        self.target: str | None = None  # who served (or, after a disconnect, who was working)
        self.reserved_usd = 0.0
        self._chars = 0
        self._settled = False

    async def reserve(self, likely_target: str) -> None:
        """Hold the estimated cost against the budget until the real cost is known."""
        self.reserved_usd = _cost(
            likely_target, self.prompt_estimate, self.estimate - self.prompt_estimate
        )
        await self.spend.add(self.key.id, self.reserved_usd)

    def _count(self, content: Any, tool_calls: Any) -> None:
        self._chars += len(content) if isinstance(content, str) else 0
        for call in tool_calls or []:
            self._chars += len((call.get("function") or {}).get("arguments") or "")

    def observe(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """Watch a streamed chunk; return what to send the client (None = drop it)."""
        if usage := chunk.get("usage"):
            self.usage = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            self._count(delta.get("content"), delta.get("tool_calls"))
        if "usage" in chunk and not self.client_wants_usage:
            if not chunk.get("choices"):
                return None  # the usage-only chunk we requested for ourselves
            chunk = {k: v for k, v in chunk.items() if k != "usage"}
        return chunk

    def observe_completion(self, result: dict[str, Any]) -> None:
        """Non-streaming answer: take its usage, and count its text in case there is none."""
        if isinstance(result.get("usage"), dict):
            self.usage = result["usage"]
        for choice in result.get("choices") or []:
            msg = choice.get("message") or {}
            self._count(msg.get("content"), msg.get("tool_calls"))

    def _actual(self) -> tuple[int, float]:
        """(tokens, USD) actually used."""
        assert self.target is not None
        usage = self.usage if isinstance(self.usage, dict) else None
        iterations = usage.get("iterations") if usage else None
        if isinstance(iterations, list) and iterations:
            # Refusal fallback (Anthropic): top-level usage covers only the attempt that
            # answered; every attempt is billed, each at its own model's rates.
            provider = self.target.partition("/")[0]
            tokens, usd = 0, 0.0
            for it in iterations:
                prompt = sum(
                    int(it.get(k) or 0)
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                completion = int(it.get("output_tokens") or 0)
                model = it.get("model")
                tokens += prompt + completion
                usd += _cost(f"{provider}/{model}" if model else self.target, prompt, completion)
            return tokens, usd
        if usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
        else:  # no usage reported (disconnect, provider without it): estimate
            prompt = self.prompt_estimate
            completion = math.ceil(self._chars / config.limits.estimation.chars_per_token)
        return prompt + completion, _cost(self.target, prompt, completion)

    async def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        tpm = self.limits.tokens_per_minute
        if self.target is None:  # nothing reached a provider: undo the reservations
            await self.limiter.adjust(self.key.id, tpm, -self.estimate)
            await self.spend.add(self.key.id, -self.reserved_usd)
            return
        tokens, usd = self._actual()
        await self.limiter.adjust(self.key.id, tpm, tokens - self.estimate)
        await self.spend.add(self.key.id, usd - self.reserved_usd)
