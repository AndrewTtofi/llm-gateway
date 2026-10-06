"""Self-healing and alerts (ADR 0019).

- **Probes.** A breaker that has been open for `open_seconds` goes half-open and waits for
  one probe request. Without this, that probe is a real user's request, which may fail.
  Here a background task sends the probe instead: a tiny request (`probe_max_tokens`
  output tokens). The breaker's probe token (one per target, fleet-wide, ADR 0004) means
  only one replica probes a target at a time. Probe cost goes to the provider account,
  not to any key, and is at most one tiny request per target per `open_seconds`.
- **Quarantine** (in the router): auth, model-not-found and quota failures hold the breaker
  open for `quarantine_seconds` instead of `open_seconds`.
- **Alerts.** Breaker state changes (and quarantines, with their reason) are posted to a
  Slack-compatible webhook (`{"text": ...}`) from the URL in `$ALERT_WEBHOOK_URL`. Each
  replica watches, but a Redis `SET NX` per target and state lets one alert through per
  `alert_min_interval_seconds`. No request content is ever included.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

import httpx

from app import config, providers
from app.observability import metrics
from app.providers.base import ProviderError, UnsupportedRequest
from app.routing import router
from app.routing.breaker import Decision, State

log = logging.getLogger(__name__)

PROBE_MESSAGES = [{"role": "user", "content": "ping"}]


def chain_targets() -> list[str]:
    """Every target a request can reach, policy candidates included."""
    return sorted(config.registry.routable_targets())


async def probe_once(target: str) -> str:
    """Probe one half-open target. → "recovered", "failed", "skipped" or "busy"."""
    reg = config.registry
    cb, sh = reg.circuit_breaker, reg.self_healing
    provider, _, model = target.partition("/")
    cfg = reg.providers.get(provider)
    if cfg is None:
        return "skipped"
    try:
        adapter = providers.pool.get(provider, cfg)
    except providers.UnsupportedProvider:
        return "skipped"
    if not adapter.configured:
        return "skipped"
    ticket = await router.store.decide(target, cb)
    if ticket.decision is not Decision.PROBE:
        return "busy"  # another replica (or a user request) holds the probe
    request = {"messages": PROBE_MESSAGES, "max_tokens": sh.probe_max_tokens}
    try:
        await asyncio.wait_for(adapter.chat(model, request), cb.probe_timeout_seconds)
    except UnsupportedRequest:
        await router.store.release(target, ticket)
        return "skipped"
    except TimeoutError:  # a hung provider: that's a failed probe, not "no verdict"
        await router.store.record_failure(target, cb, ticket)
        return "failed"
    except ProviderError as exc:
        if router.classify(exc, reg.retry) is router.Kind.CLIENT:
            await router.store.record_success(target, cb, ticket)  # it answered
            return "recovered"
        await router.store.record_failure(target, cb, ticket)
        if reason := router._quarantine_reason(exc, reg):
            await router.store.quarantine(target, cb, sh.quarantine_seconds, reason)
        return "failed"
    except Exception:  # timeout, cancellation, bug: no verdict
        await router.store.release(target, ticket)
        raise
    await router.store.record_success(target, cb, ticket)
    return "recovered"


async def probe_loop() -> None:
    while True:
        sh = config.registry.self_healing
        if sh.probes:
            for target in chain_targets():
                try:
                    if await router.store.state(target) != State.HALF_OPEN:
                        continue
                    result = await probe_once(target)
                    metrics.probes.labels(target, result).inc()
                    if result != "busy":
                        log.info("background probe of %s: %s", target, result)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("background probe of %s failed", target)
        await asyncio.sleep(sh.probe_interval_seconds)


class Alerts:
    """Turns breaker state observations into webhook posts (deduplicated fleet-wide)."""

    def __init__(self, redis: Any = None, post: Any = None) -> None:
        self.redis = redis
        self.known: dict[str, str] = {}
        self._post = post  # tests inject this; default: httpx
        self.host = socket.gethostname()
        self._tasks: set[asyncio.Task[None]] = set()

    async def drain(self) -> None:
        """Wait for alerts being sent (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def observe(self, target: str, state: str) -> None:
        before = self.known.get(target)
        self.known[target] = state
        if before is None or before == state:
            return  # first sight (startup) or no change
        if state == State.HALF_OPEN:
            return  # a step on the way to open or closed: noise on its own
        reason = await router.store.reason(target) if state != State.CLOSED else None
        # In the background: a slow webhook mustn't hold up polling the other targets.
        task = asyncio.create_task(self.send(target, before, state, reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _first_to_send(self, target: str, state: str) -> bool:
        interval = config.registry.self_healing.alert_min_interval_seconds
        if self.redis is None or interval <= 0:
            return True
        try:
            key = f"alert:{{{target}}}:{state}"
            return bool(await self.redis.set(key, self.host, nx=True, ex=max(1, round(interval))))
        except Exception:
            return True  # Redis down: rather a duplicate alert than none

    async def send(self, target: str, before: str, after: str, reason: str | None) -> None:
        env = config.registry.self_healing.alert_webhook_env
        url = os.environ.get(env, "") if env else ""
        if not url or not await self._first_to_send(target, after):
            return
        icon = {"open": "🔴", "half_open": "🟡", "closed": "🟢"}.get(after, "⚪")
        text = f"{icon} LLM gateway: {target} circuit {before} → {after}"
        if reason:
            text += f" ({reason})"
        payload = {"text": text, "target": target, "from": before, "to": after, "reason": reason}
        try:
            if self._post is not None:
                await self._post(url, payload)
            else:
                async with httpx.AsyncClient(timeout=5) as http:
                    (await http.post(url, json=payload)).raise_for_status()
            metrics.alerts.labels("sent").inc()
        except Exception as exc:
            metrics.alerts.labels("failed").inc()
            log.warning("alert webhook failed: %s", type(exc).__name__)
