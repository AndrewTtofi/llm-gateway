"""Per-request accounting (ADR 0007, 0008): estimate → reserve → reconcile → record.

At admission the token bucket is charged an estimate and the estimated cost is
*reserved* against the budget, so a burst of long requests can't all slip in under a
stale month-to-date figure. `settle()` runs exactly once when the request ends —
success, failure or disconnect — replaces both with the real numbers, and records the
request: Prometheus metrics, a usage-log row and one access log line.

When the provider reports no usage because the request was cut off (client hang-up,
timeout), the bill is an estimate that can't be gamed by hanging up early (ADR 0023):
everything relayed (answer, tool calls, thinking), and at least the time the provider
spent generating at an assumed speed, up to the reserved maximum.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
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


def _cost(
    target: str,
    prompt: int,
    completion: int,
    cached: int = 0,
    written: int = 0,
    written_1h: int = 0,
    at: datetime | None = None,
) -> float | None:
    cost = config.pricing.cost(target, prompt, completion, cached, written, written_1h, at)
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

    def add(self, other: Used) -> None:
        self.prompt += other.prompt
        self.completion += other.completion
        self.cached += other.cached
        self.usd += other.usd
        self.unpriced = self.unpriced or other.unpriced
        self.estimated = self.estimated or other.estimated


def _text_chars(part: dict[str, Any]) -> int:
    """Characters of output in a streamed delta or a message: the answer, tool-call
    arguments, and reasoning (Anthropic thinking, `reasoning_content`, `reasoning`)."""
    n = len(c) if isinstance(c := part.get("content"), str) else 0
    for call in part.get("tool_calls") or []:
        if isinstance(call, dict):
            n += len((call.get("function") or {}).get("arguments") or "")
    for name in ("reasoning_content", "reasoning"):  # DeepSeek, OpenRouter, vLLM …
        if isinstance(r := part.get(name), str):
            n += len(r)
    thinking = part.get("thinking")  # a streamed thinking delta (extension, ADR 0013)
    if isinstance(thinking, dict) and isinstance(t := thinking.get("thinking"), str):
        n += len(t)
    for block in part.get("thinking_blocks") or []:
        if isinstance(block, dict) and isinstance(t := block.get("thinking"), str):
            n += len(t)
    return n


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
        alias_label: str | None = None,
    ) -> None:
        # The metric label must be a bounded value: the alias as configured, or
        # "_unknown" — never arbitrary client input (see main._chat).
        self.alias_label = alias if alias_label is None else alias_label
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
        self.client_request_id = obs_log.client_request_id.get()
        self.started_at = datetime.now(UTC)  # time-of-day prices apply at the request's start
        # From the request arriving (middleware), so admission counts too.
        self.started = obs_log.request_started.get() or time.perf_counter()
        self.first_chunk_at: float | None = None
        self.status = 200
        self.error_code: str | None = None
        self.attempts: list[tuple[str, str]] = []
        self.fallback = False
        self.attempt_started: float | None = None  # start of the attempt that served
        self.timed_out: list[tuple[str, float]] = []  # other attempts the provider billed
        self.release: Callable[[], None] | None = None  # frees the key's concurrency slot
        self.stream_done = False
        # Response cache (ADR 0018): where to store the answer, and what was served from it.
        self.cache: Any = None  # app.cache.Lookup
        self.cache_hit: str | None = None  # "cache/exact" … when the answer came from cache
        self.collector: Any = None  # app.cache.Collector, assembling a streamed answer
        self.cache_result: dict[str, Any] | None = None  # a non-streamed answer to store
        self.variant: str | None = None  # A/B arm (ADR 0020)
        # LLM-as-judge (ADR 0022): set when this request was sampled for judging.
        self.judge_cfg: Any = None
        self.judge_conversation = ""

    async def reserve(self, likely_target: str) -> None:
        """Hold the estimated cost against the budget until the real cost is known."""
        self.reserved_usd = (
            _cost(likely_target, self.prompt_estimate, self.estimate - self.prompt_estimate) or 0.0
        )
        await self._add_spend(self.reserved_usd)

    async def _add_spend(self, usd: float) -> None:
        """Month-to-date spend for the key and, if it has one, its team."""
        await self.spend.add(self.key.id, usd)
        if team := self.key.team_spend_id:
            await self.spend.add(team, usd)

    def failed(self, code: str) -> None:
        """The stream ended with an in-band error (after the 200 went out)."""
        self.error_code = code

    def finished(self) -> None:
        """The stream delivered everything (about to send [DONE])."""
        self.stream_done = True

    def routed(self, routed: Any) -> None:
        self.attempts, self.fallback = list(routed.attempts), routed.fallback
        self.attempt_started = routed.attempt_started or None
        self.timed_out = list(routed.timed_out)

    def observe(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """Watch a streamed chunk; return what to send the client (None = drop it)."""
        if self.collector is not None:
            self.collector.feed(chunk)

        if usage := chunk.get("usage"):
            self.usage = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if self.first_chunk_at is None and (delta.get("content") or delta.get("tool_calls")):
                # TTFT = first chunk carrying output (not the empty role chunk) — the same
                # definition the load generator uses.
                self.first_chunk_at = time.perf_counter()
            self._chars += _text_chars(delta)
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
            self._chars += _text_chars(choice.get("message") or {})

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
                written = int(it.get("cache_creation_input_tokens") or 0)
                written_1h = int(
                    (it.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                )
                prompt = cached + written + int(it.get("input_tokens") or 0)
                completion = int(it.get("output_tokens") or 0)
                model = it.get("model")
                usd = _cost(
                    f"{provider}/{model}" if model else self.target,
                    prompt,
                    completion,
                    cached,
                    written,
                    written_1h,
                    self.started_at,
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
            details = details if isinstance(details, dict) else {}
            cached = int(details.get("cached_tokens") or 0)
            written = int(details.get("cache_creation_tokens") or 0)  # Anthropic (ADR 0013)
            written_1h = int(details.get("cache_creation_1h_tokens") or 0)
            usd = _cost(
                self.target, prompt, completion, cached, written, written_1h, self.started_at
            )
            return Used(prompt, completion, cached, usd or 0.0, unpriced=usd is None)
        # No usage reported (cut off, or a provider without it): estimate.
        completion = math.ceil(self._chars / config.limits.estimation.chars_per_token)
        if self._interrupted():
            ran = time.perf_counter() - (self.attempt_started or self.started)
            completion = max(completion, self._generated_in(ran))
        return self._estimated(self.target, completion)

    def _interrupted(self) -> bool:
        """Cut off before the provider finished: it may have generated (and billed) more
        than reached us, including reasoning it never streams."""
        if self.error_code in ("client_disconnected", "upstream_timeout"):
            return True
        return self.streamed and self.status == 200 and not self.error_code and not self.stream_done

    def _generated_in(self, seconds: float) -> int:
        """Output tokens a provider could have generated in `seconds`, at most the cap."""
        est = config.limits.estimation
        cap = max(0, self.estimate - self.prompt_estimate)
        return min(cap, math.ceil(seconds * est.output_tokens_per_second))

    def _estimated(self, target: str, completion: int) -> Used:
        prompt = self.prompt_estimate
        usd = _cost(target, prompt, completion, at=self.started_at)
        return Used(prompt, completion, 0, usd or 0.0, unpriced=usd is None, estimated=True)

    async def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        if self.release is not None:
            self.release()
        tpm = self.limits.tokens_per_minute
        used, extra = Used(), []
        try:
            if self.target is None and not self.timed_out:
                # Nothing reached a provider: undo the reservations.
                await self.limiter.adjust(self.key.id, tpm, -self.estimate)
                await self._add_spend(-self.reserved_usd)
                return
            if self.target is not None:
                used = self._actual()
            # A timed-out attempt was still processed, and billed, by its provider.
            extra = [(t, self._estimated(t, self._generated_in(s))) for t, s in self.timed_out]
            total = Used()
            for u in (used, *(u for _, u in extra)):
                total.add(u)
            await self.limiter.adjust(self.key.id, tpm, total.tokens - self.estimate)
            await self._add_spend(total.usd - self.reserved_usd)
            if self.target is not None:
                await self._store_in_cache()
                self._submit_for_judging()
        finally:
            # Recorded even if Redis just failed: that's when you most need the data.
            self._record(used, extra)

    def _answer(self) -> dict[str, Any] | None:
        """The finished answer (non-streamed, or assembled from a clean stream)."""
        if self.status != 200 or self.error_code:
            return None
        if self.streamed:
            return (
                self.collector.result() if self.collector is not None and self.stream_done else None
            )
        return self.cache_result

    def _submit_for_judging(self) -> None:
        if self.judge_cfg is None:
            return
        try:
            from app import judge, services

            if (answer := self._answer()) is None:
                return
            services.judge.submit(
                judge.Job(
                    self.request_id,
                    self.alias_label,
                    self.target,
                    self.variant,
                    self.judge_cfg,
                    self.judge_conversation,
                    judge.answer_text(answer),
                )
            )
        except Exception:
            log.exception("submitting for judging failed")

    async def _store_in_cache(self) -> None:
        """A clean, complete answer goes into the response cache (if the alias has one)."""
        if self.cache is None or self.status != 200 or self.error_code:
            return
        if self.streamed:
            if self.collector is None or not self.stream_done:
                return
            await self.cache.save(self.collector.result())
        elif self.cache_result is not None:
            await self.cache.save(self.cache_result)

    def _record(self, served: Used, extra: list[tuple[str, Used]]) -> None:
        """Metrics, usage row, access line. Never raises; never logs content. `served` is
        what the target that answered used; `extra`, timed-out attempts on the way."""
        now = time.perf_counter()
        used = Used()
        for u in (served, *(u for _, u in extra)):
            used.add(u)
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
        target = self.target or self.cache_hit or ""
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
                metrics.tokens.labels(target, "prompt").inc(served.prompt)
                metrics.tokens.labels(target, "completion").inc(served.completion)
                metrics.tokens.labels(target, "cached").inc(served.cached)
                if served.usd:
                    metrics.cost.labels(target).inc(served.usd)
                if self.fallback:
                    metrics.fallbacks.labels(self.alias_label, target).inc()
            for t, u in extra:
                metrics.tokens.labels(t, "prompt").inc(u.prompt)
                metrics.tokens.labels(t, "completion").inc(u.completion)
                if u.usd:
                    metrics.cost.labels(t).inc(u.usd)
        except Exception:
            log.exception("recording metrics failed")
        if self.variant:
            try:
                metrics.variant_requests.labels(self.alias_label, self.variant, status_label).inc()
                metrics.variant_duration.labels(self.alias_label, self.variant).observe(latency)
                if used.usd:
                    metrics.variant_cost.labels(self.alias_label, self.variant).inc(used.usd)
            except Exception:
                log.exception("recording variant metrics failed")
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
            client_request_id=self.client_request_id,
            key_id=self.key.id,
            key_prefix=self.key.prefix,
            team=self.key.team,
            variant=self.variant,
            alias=self.alias,
            target=self.target or self.cache_hit,
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
            target=self.target or self.cache_hit,
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
