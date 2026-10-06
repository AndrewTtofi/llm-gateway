"""Per-request accounting (ADR 0007, 0008): estimate → reserve → reconcile → record.

At admission the token bucket is charged an estimate and the estimated cost is
*reserved* against the budget, so a burst of long requests can't all slip in under a
stale month-to-date figure. `settle()` runs exactly once when the request ends —
success, failure or disconnect — replaces both with the real numbers, and records the
request: Prometheus metrics, a usage-log row and one access log line.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import config
from app.auth import ApiKey, EffectiveLimits
from app.observability import live, metrics
from app.observability import logging as obs_log
from app.observability.usage import UsageRecord, UsageSink
from app.ratelimit import Limiter, SpendTracker

log = logging.getLogger(__name__)
_unpriced_warned: set[str] = set()


def _cost(target: str, prompt: int, completion: int, cached: int = 0) -> float | None:
    cost = config.pricing.cost(target, prompt, completion, cached)
    if cost is None and target not in _unpriced_warned:
        _unpriced_warned.add(target)
        log.warning("no price for %s in pricing.yaml; counting it as $0", target)
    return cost


@dataclass
class Used:
    prompt: int = 0  # includes cached
    completion: int = 0
    cached: int = 0
    usd: float = 0.0  # what the priced part cost (budgets charge this)
    unpriced: bool = False  # some (or all) of it had no price in pricing.yaml
    estimated: bool = False  # no provider usage: estimated from characters

    @property
    def cost_for_record(self) -> float | None:
        return None if self.unpriced and not self.usd else self.usd

    @property
    def tokens(self) -> int:
        return self.prompt + self.completion


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
        sink: UsageSink | None = None,
        alias: str = "",
        streamed: bool = False,
    ) -> None:
        # The metric label must be a bounded value: the alias as configured, or
        # "_unknown" — never arbitrary client input (see main.chat_completions).
        self.alias_label = alias
        self.key, self.limits, self.limiter, self.spend = key, limits, limiter, spend
        self.estimate, self.prompt_estimate = estimate, prompt_estimate
        self.client_wants_usage = client_wants_usage
        self.usage: dict[str, Any] | None = None
        self.target: str | None = None  # who served (or, after a disconnect, who was working)
        self.reserved_usd = 0.0
        self._chars = 0
        self._settled = False
        # For the record (ADR 0008):
        self.sink, self.alias, self.streamed = sink, alias, streamed
        self.request_id = obs_log.request_id.get()
        # From the request arriving (middleware), so admission counts too.
        self.started = obs_log.request_started.get() or time.perf_counter()
        self.first_chunk_at: float | None = None
        self.status = 200
        self.error_code: str | None = None
        self.attempts: list[tuple[str, str]] = []
        self.fallback = False
        self.attempt_started: float | None = None  # start of the attempt that served
        self.stream_done = False

    async def reserve(self, likely_target: str) -> None:
        """Hold the estimated cost against the budget until the real cost is known."""
        self.reserved_usd = (
            _cost(likely_target, self.prompt_estimate, self.estimate - self.prompt_estimate) or 0.0
        )
        await self.spend.add(self.key.id, self.reserved_usd)

    def _count(self, content: Any, tool_calls: Any) -> None:
        self._chars += len(content) if isinstance(content, str) else 0
        for call in tool_calls or []:
            self._chars += len((call.get("function") or {}).get("arguments") or "")

    def failed(self, code: str) -> None:
        """The stream ended with an in-band error (after the 200 went out)."""
        self.error_code = code

    def finished(self) -> None:
        """The stream delivered everything (about to send [DONE])."""
        self.stream_done = True

    def routed(self, routed: Any) -> None:
        self.attempts, self.fallback = list(routed.attempts), routed.fallback
        self.attempt_started = routed.attempt_started or None

    def observe(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """Watch a streamed chunk; return what to send the client (None = drop it)."""

        if usage := chunk.get("usage"):
            self.usage = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if self.first_chunk_at is None and (delta.get("content") or delta.get("tool_calls")):
                # TTFT = first chunk carrying output (not the empty role chunk) — the same
                # definition the load generator uses.
                self.first_chunk_at = time.perf_counter()
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

    def _actual(self) -> Used:
        assert self.target is not None
        usage = self.usage if isinstance(self.usage, dict) else None
        iterations = usage.get("iterations") if usage else None
        if isinstance(iterations, list) and iterations:
            # Refusal fallback (Anthropic): top-level usage covers only the attempt that
            # answered; every attempt is billed, each at its own model's rates.
            provider = self.target.partition("/")[0]
            used = Used()
            for it in iterations:
                cached = int(it.get("cache_read_input_tokens") or 0)
                prompt = cached + sum(
                    int(it.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens")
                )
                completion = int(it.get("output_tokens") or 0)
                model = it.get("model")
                usd = _cost(
                    f"{provider}/{model}" if model else self.target, prompt, completion, cached
                )
                used.prompt += prompt
                used.completion += completion
                used.cached += cached
                if usd is None:
                    used.unpriced = True  # still bill the attempts that do have a price
                else:
                    used.usd += usd
            return used
        if usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details")
            cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
            usd = _cost(self.target, prompt, completion, cached)
            return Used(prompt, completion, cached, usd or 0.0, unpriced=usd is None)
        # No usage reported (disconnect, provider without it): estimate.
        prompt = self.prompt_estimate
        completion = math.ceil(self._chars / config.limits.estimation.chars_per_token)
        usd = _cost(self.target, prompt, completion)
        return Used(prompt, completion, 0, usd or 0.0, unpriced=usd is None, estimated=True)

    async def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        tpm = self.limits.tokens_per_minute
        used = Used()
        try:
            if self.target is None:  # nothing reached a provider: undo the reservations
                await self.limiter.adjust(self.key.id, tpm, -self.estimate)
                await self.spend.add(self.key.id, -self.reserved_usd)
                return
            used = self._actual()
            await self.limiter.adjust(self.key.id, tpm, used.tokens - self.estimate)
            await self.spend.add(self.key.id, used.usd - self.reserved_usd)
        finally:
            # Recorded even if Redis just failed: that's when you most need the data.
            self._record(used)

    def _record(self, used: Used) -> None:
        """Metrics, usage row, access line. Never raises; never logs content."""
        now = time.perf_counter()
        if (
            self.streamed
            and self.target
            and self.status == 200
            and not self.error_code
            and not self.stream_done
        ):
            self.status, self.error_code = 499, "client_disconnected"  # left mid-stream
        # Usage row: what the client experienced, end to end (fallbacks included).
        latency = now - self.started
        ttft = (
            self.first_chunk_at - self.started
            if self.streamed and self.first_chunk_at is not None
            else None
        )
        # Histograms per target: only the attempt that served, so a healthy fallback
        # isn't blamed for the failed provider's timeouts.
        served_from = self.attempt_started or self.started
        target_latency = now - served_from
        target_ttft = (
            self.first_chunk_at - served_from
            if self.streamed and self.first_chunk_at is not None
            else None
        )
        target = self.target or ""
        status_label = (
            str(self.status) if not self.error_code else f"{self.status}:{self.error_code}"
        )
        try:
            metrics.requests.labels(self.alias_label, target, status_label).inc()
            for t, outcome in self.attempts:
                metrics.attempts.labels(t, outcome).inc()
            if target:
                metrics.duration.labels(target, str(self.streamed).lower()).observe(target_latency)
                if target_ttft is not None:
                    metrics.ttft.labels(target).observe(target_ttft)
                if ttft is not None:
                    metrics.ttft_e2e.observe(ttft)
                metrics.tokens.labels(target, "prompt").inc(used.prompt)
                metrics.tokens.labels(target, "completion").inc(used.completion)
                metrics.tokens.labels(target, "cached").inc(used.cached)
                if used.usd:
                    metrics.cost.labels(target).inc(used.usd)
                if self.fallback:
                    metrics.fallbacks.labels(self.alias_label, target).inc()
        except Exception:
            log.exception("recording metrics failed")
        try:  # separately, so a bug here can't cost the metrics above
            for t, outcome in self.attempts:
                if not outcome.startswith(("skipped:", "unsupported")):
                    # A client-fault answer means the provider is healthy (ADR 0004).
                    live.record_attempt(t, outcome == "ok" or outcome.startswith("client:"))
            if target and self.status < 400 and not self.error_code:
                live.record_served(target, target_latency, target_ttft)
        except Exception:
            log.exception("recording live stats failed")
        record = UsageRecord(
            created_at=datetime.now(UTC),
            request_id=self.request_id,
            key_id=self.key.id,
            key_prefix=self.key.prefix,
            alias=self.alias,
            target=self.target,
            status=self.status,
            error_code=self.error_code,
            streamed=self.streamed,
            fallback=self.fallback,
            attempts=sum(1 for _, o in self.attempts if not o.startswith(("skipped", "unsup"))),
            prompt_tokens=used.prompt,
            completion_tokens=used.completion,
            cached_tokens=used.cached,
            usage_estimated=used.estimated,
            cost_usd=used.cost_for_record,
            latency_ms=round(latency * 1000),
            ttft_ms=round(ttft * 1000) if ttft else None,
            estimated_tokens=self.estimate,
        )
        if self.sink is not None:
            self.sink.submit(record)
        obs_log.usage.info(
            "request",
            key=self.key.prefix,
            alias=self.alias,
            target=self.target,
            status=self.status,
            error=self.error_code,
            stream=self.streamed,
            fallback=self.fallback,
            attempts=record.attempts,
            prompt_tokens=used.prompt,
            completion_tokens=used.completion,
            cached_tokens=used.cached,
            cost_usd=used.cost_for_record,
            latency_ms=record.latency_ms,
            ttft_ms=record.ttft_ms,
        )
