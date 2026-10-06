"""Route a request through its alias chain: retry, fall back, respect breakers (ADR 0004).

for each target in the chain:
    skip it if its circuit breaker is open
    try it (up to max_attempts_per_provider, backoff + jitter between tries)
    success → done · client fault → return the error · otherwise → next target
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app import config, providers
from app.config import Registry, RetryConfig
from app.providers import UnsupportedProvider
from app.providers.base import (
    CLIENT_FAULT_STATUS,
    QUOTA_CODES,
    NotConfigured,
    ProviderAdapter,
    ProviderError,
    UnsupportedRequest,
)
from app.routing.breaker import BreakerStore, Decision, MemoryBreakerStore, Ticket
from app.schemas import ChatCompletionRequest

log = logging.getLogger(__name__)

# Replaced at startup with the configured store (see main.lifespan); memory by default.
store: BreakerStore = MemoryBreakerStore()


class Kind(StrEnum):
    CLIENT = "client"  # the request is wrong: every provider would refuse it
    GATEWAY = "gateway"  # this target can't serve (auth, missing model, quota): next one
    TRANSIENT = "transient"  # might work if tried again: retry, then next one


def classify(exc: ProviderError, retry: RetryConfig) -> Kind:
    if exc.code in QUOTA_CODES:
        return Kind.GATEWAY
    if exc.status in CLIENT_FAULT_STATUS:
        return Kind.CLIENT
    if exc.timeout or (exc.status is None and exc.retryable):
        return Kind.TRANSIENT
    if exc.status in retry.retry_on_status:
        return Kind.TRANSIENT
    return Kind.GATEWAY


def backoff_seconds(attempt: int, retry: RetryConfig, exc: ProviderError) -> float | None:
    """Delay before retry number `attempt` (0-based), or None to skip to the next target.

    Full jitter — random(0, min(max, base·2^attempt)) — spreads retries out so many
    clients failing at once don't come back in lockstep and re-overload the provider.
    """
    retry_after = exc.headers.get("retry-after")
    if retry_after is not None:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = None
        if wait is not None:
            # The provider told us how long. If it's longer than we'd ever wait, don't
            # block the client: the next target is the better bet. A little jitter on
            # top stops everyone told "2s" from retrying in the same millisecond.
            if wait * 1000 > retry.backoff_max_ms:
                return None
            return wait + random.uniform(0, 0.1 * wait)  # noqa: S311 — jitter, not crypto
    cap = min(retry.backoff_max_ms, retry.backoff_base_ms * 2**attempt)
    return random.uniform(0, cap) / 1000  # noqa: S311


class UnknownModel(LookupError):
    pass


SKIPPED = (
    "skipped:open",
    "skipped:unsupported",
    "skipped:unconfigured",
    "skipped:busy",
    "unsupported_request",
)


@dataclass
class Routed:
    """Where a request ended up, plus every attempt on the way (for headers and logs)."""

    target: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list)
    current: str = ""  # target being tried right now (who to bill if the client leaves)
    chain: list[str] | None = None  # set by policy routing (ADR 0017): this request's chain
    attempt_started: float = 0.0  # perf_counter at the start of the latest attempt
    # Attempts that timed out after reaching the provider, and how long each ran. The
    # provider bills those, so the key is charged for them too (ADR 0023).
    timed_out: list[tuple[str, float]] = field(default_factory=list)

    @property
    def fallback(self) -> bool:
        """Served by a target other than the first one tried (not: failed everywhere)."""
        return bool(self.target) and bool(self.attempts) and self.attempts[0][0] != self.target

    @property
    def calls(self) -> int:
        """Upstream calls actually made (skips and untranslatable requests aren't calls)."""
        return sum(1 for _, outcome in self.attempts if outcome not in SKIPPED)

    def headers(self) -> dict[str, str]:
        out = {"x-gateway-attempts": str(self.calls)}
        if self.target:
            out["x-gateway-provider"] = self.target
            out["x-gateway-fallback"] = "true" if self.fallback else "false"
        return out


class AllTargetsFailed(Exception):
    def __init__(self, routed: Routed, last: Exception | None, all_open: bool):
        super().__init__("all targets failed")
        self.routed, self.last, self.all_open = routed, last, all_open


async def _route[T](
    body: ChatCompletionRequest,
    call: Callable[[ProviderAdapter, str, dict[str, Any]], Awaitable[T]],
    routed: Routed | None = None,
) -> tuple[T, Routed, ProviderAdapter, Registry]:
    reg = config.registry  # one snapshot for the whole request, even across a reload
    routed = routed if routed is not None else Routed()
    if routed.chain:
        chain = routed.chain
    else:
        try:
            chain = reg.resolve(body.model)
        except KeyError as exc:
            raise UnknownModel(body.model) from exc
    # The error to report if nothing works. A provider failing outranks "this provider
    # can't express the request": the former is what actually stopped us.
    provider_error: ProviderError | None = None
    other_error: Exception | None = None
    skipped_open = False
    unconfigured = False

    for target in chain:
        provider, _, model = target.partition("/")
        cfg = reg.providers.get(provider)
        try:
            if cfg is None:
                raise UnsupportedProvider(f"provider {provider!r} is not configured")
            adapter = providers.pool.get(provider, cfg)
        except UnsupportedProvider as exc:
            routed.attempts.append((target, "skipped:unsupported"))
            other_error = other_error or exc
            continue
        if not adapter.configured:
            # No credentials: not a failure of the target, so no call and no breaker
            # change. It shouldn't open, flap or show as "open" for being unconfigured.
            routed.attempts.append((target, "skipped:unconfigured"))
            unconfigured = True
            continue

        ticket = await store.decide(target, reg.circuit_breaker)
        if ticket.decision is Decision.DENY:
            routed.attempts.append((target, "skipped:open"))
            skipped_open = True
            continue
        result = await _try_target(target, adapter, model, body, call, reg, ticket, routed)
        if isinstance(result, _Failed):
            err = result.error
            if isinstance(err, ProviderError):
                # Report a real provider failure over "this replica's pool was full".
                if provider_error is None or not err.local or provider_error.local:
                    provider_error = err
            else:
                other_error = result.error
            continue
        routed.target = target
        if routed.fallback:
            log.warning("served by fallback %s after %s", target, routed.attempts)
        return result.value, routed, adapter, reg

    if provider_error is not None:
        raise AllTargetsFailed(routed, provider_error, all_open=False)
    if skipped_open:  # nothing actually failed; healthy-but-open targets will be back
        raise AllTargetsFailed(routed, None, all_open=True)
    if other_error is None and unconfigured:
        other_error = NotConfigured(body.model)  # every target lacks credentials
    raise AllTargetsFailed(routed, other_error, all_open=False)


def _quarantine_reason(exc: ProviderError, reg: Registry) -> str | None:
    """A fixed, bounded description (it ends up in alerts), or None to not quarantine."""
    if exc.code in QUOTA_CODES:
        return "quota exhausted"
    if exc.status in reg.self_healing.quarantine_status:
        return {401: "authentication failed", 403: "permission denied", 404: "model not found"}.get(
            exc.status, f"HTTP {exc.status}"
        )
    return None


@dataclass
class _Ok[T]:
    value: T


@dataclass
class _Failed:
    error: Exception


async def _try_target[T](
    target: str,
    adapter: ProviderAdapter,
    model: str,
    body: ChatCompletionRequest,
    call: Callable[[ProviderAdapter, str, dict[str, Any]], Awaitable[T]],
    reg: Registry,
    ticket: Ticket,
    routed: Routed,
) -> _Ok[T] | _Failed:
    """All attempts on one target. Every exit path settles a half-open probe: success,
    failure, or — when there's no verdict (cancelled, unsupported) — handing the slot
    back so the target isn't blocked until the probe times out."""
    cb = reg.circuit_breaker
    settled = False
    counted = False  # one request counts against a target's breaker at most once
    # A half-open probe gets one try: if the target is still sick, fail fast.
    tries = 1 if ticket.decision is Decision.PROBE else reg.retry.max_attempts_per_provider
    try:
        for attempt in range(tries):
            routed.attempt_started = time.perf_counter()
            routed.current = target  # in flight: billed if the client leaves now
            try:
                value = await call(adapter, model, body.upstream_body(model))
            except UnsupportedRequest as exc:
                routed.current = ""
                # Can't be expressed for this provider (e.g. n>1 on Anthropic); another
                # provider may handle it. Says nothing about the target's health.
                routed.attempts.append((target, "unsupported_request"))
                return _Failed(exc)
            except ProviderError as exc:
                # The attempt is over: a hang-up from here (backoff, next target) mustn't
                # bill it again. A timed-out attempt is billed through `timed_out`.
                routed.current = ""
                if exc.local:
                    # No free connection: never sent, so not the target's fault (ADR 0023).
                    routed.attempts.append((target, "skipped:busy"))
                    return _Failed(exc)
                kind = classify(exc, reg.retry)
                # A fixed set of labels: HTTP status, or timeout/network — never a
                # provider-supplied code (unbounded metric cardinality).
                detail = exc.status or ("timeout" if exc.timeout else "network")
                routed.attempts.append((target, f"{kind}:{detail}"))
                if exc.timeout:
                    routed.timed_out.append((target, time.perf_counter() - routed.attempt_started))
                if exc.deadline:
                    # The answer outlasted the gateway's limit for a whole request. The
                    # client's request decides how long that takes, so it says nothing
                    # about the target's health; retrying would only repeat it.
                    return _Failed(exc)
                if kind is Kind.CLIENT:
                    # The provider answered — it's healthy, the request is bad.
                    await store.record_success(target, cb, ticket)
                    settled = True
                    raise AllTargetsFailed(routed, exc, all_open=False) from exc
                if ticket.decision is Decision.PROBE or not counted:
                    if await store.record_failure(target, cb, ticket):
                        log.warning("circuit opened for %s", target)
                    counted = True
                settled = True
                if kind is Kind.GATEWAY and (reason := _quarantine_reason(exc, reg)):
                    # Bad key, unknown model, exhausted quota: won't heal in seconds.
                    sh = reg.self_healing
                    await store.quarantine(target, cb, sh.quarantine_seconds, reason)
                    log.warning("quarantined %s for %ss: %s", target, sh.quarantine_seconds, reason)
                if kind is Kind.TRANSIENT and attempt + 1 < tries:
                    delay = backoff_seconds(attempt, reg.retry, exc)
                    if delay is not None:
                        await asyncio.sleep(delay)
                        continue
                return _Failed(exc)
            else:
                await store.record_success(target, cb, ticket)
                settled = True
                routed.attempts.append((target, "ok"))
                return _Ok(value)
        raise AssertionError("unreachable: the loop always returns")  # pragma: no cover
    finally:
        if not settled:
            await store.release(target, ticket)


async def route_chat(
    body: ChatCompletionRequest, routed: Routed | None = None
) -> tuple[dict[str, Any], Routed]:
    async def call(adapter: ProviderAdapter, model: str, req: dict[str, Any]) -> dict[str, Any]:
        return await adapter.chat(model, req)

    value, routed, _, _ = await _route(body, call, routed)
    return value, routed


class CommittedStream:
    """The rest of a stream after its first chunk: the target is fixed (ADR 0005).

    Enforces `stream_total`, tells the breaker about mid-stream failures, and — unlike a
    wrapping async generator, whose `aclose()` does nothing if it never started —
    always closes the upstream when closed.
    """

    def __init__(self, chunks: Any, target: str, reg: Registry, stream_total: float) -> None:
        self._chunks = chunks
        self._target = target
        self._reg = reg
        self._deadline = asyncio.get_running_loop().time() + stream_total

    def __aiter__(self) -> CommittedStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            # Timeout only around the upstream read, never across the caller's work.
            async with asyncio.timeout_at(self._deadline):
                chunk: dict[str, Any] = await anext(self._chunks)
            return chunk
        except TimeoutError as exc:
            await self.aclose()
            err = ProviderError(
                f"{self._target.partition('/')[0]} stream exceeded its time limit",
                timeout=True,
                deadline=True,
            )
            await self._record(err)
            raise err from exc
        except ProviderError as exc:
            await self._record(exc)
            raise

    async def _record(self, exc: ProviderError) -> None:
        if exc.deadline or exc.local:
            return  # not the target's health (ADR 0023)
        if classify(exc, self._reg.retry) is not Kind.CLIENT:
            await store.record_failure(
                self._target, self._reg.circuit_breaker, Ticket(Decision.ALLOW)
            )

    async def aclose(self) -> None:
        await self._chunks.aclose()


Stream = tuple[list[dict[str, Any]], CommittedStream]


def has_output(chunk: dict[str, Any]) -> bool:
    """A chunk that carries the model's output (text, tool calls, reasoning), or ends the
    answer. Role-only opening chunks (Anthropic's message_start, the Responses API's
    response.created, OpenAI's first delta) don't: a failure after them can still fall back."""
    if chunk.get("usage") and not chunk.get("choices"):
        return True
    for choice in chunk.get("choices") or []:
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("tool_calls") or delta.get("thinking"):
            return True
        if delta.get("reasoning_content") or delta.get("reasoning") or delta.get("refusal"):
            return True
    return False


async def route_stream(
    body: ChatCompletionRequest, routed: Routed | None = None
) -> tuple[Stream, Routed]:
    """Retry/fallback cover everything up to the first chunk of real output; after that
    the stream is committed to its target (ADR 0005). Opening chunks before it are held
    back and sent with it. `first_output` (default: `first_token`) bounds the wait."""

    async def call(adapter: ProviderAdapter, model: str, req: dict[str, Any]) -> Any:
        chunks = adapter.stream(model, req)
        t = adapter.cfg.get("timeouts", {})
        wait = float(t.get("first_output", t.get("first_token", 30)))
        held: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(wait):
                async for chunk in chunks:  # raises → this target failed before output
                    held.append(chunk)
                    if has_output(chunk):
                        break
        except TimeoutError as exc:
            await chunks.aclose()
            raise ProviderError(
                f"{adapter.name} sent no output within {wait:g}s", retryable=True, timeout=True
            ) from exc
        except BaseException:
            await chunks.aclose()
            raise
        return held, chunks

    (first, chunks), routed, adapter, reg = await _route(body, call, routed)
    try:
        stream_total = float(adapter.cfg.get("timeouts", {}).get("stream_total", 900))
        return (first, CommittedStream(chunks, routed.target, reg, stream_total)), routed
    except BaseException:
        await chunks.aclose()
        raise
