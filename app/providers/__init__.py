"""Builds and caches one adapter (and its HTTP connection pool) per configured provider."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app import config
from app.providers.anthropic import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.fake import FakeAdapter
from app.providers.openai_compat import OpenAICompatAdapter

log = logging.getLogger(__name__)

ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAICompatAdapter,
    "anthropic": AnthropicAdapter,
    "fake": FakeAdapter,
}


class UnsupportedProvider(Exception):
    pass


class AdapterPool:
    """Adapters are reused across requests so connections stay warm.

    On config reload a provider whose config changed gets a fresh adapter. The old one
    may still be serving in-flight requests, so it is closed only after a grace period
    longer than the slowest request it could be serving.

    Requests that started before a reload still pass the old config. They get the retired
    adapter for it, never a new one: rebuilding on every such call would flip the live
    adapter back and forth and multiply open connections (each adapter has its own pool).
    """

    def __init__(self) -> None:
        self._live: dict[str, tuple[str, ProviderAdapter]] = {}
        self._retired: set[ProviderAdapter] = set()
        self._by_config: dict[tuple[str, str], ProviderAdapter] = {}  # retired, still open
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _fingerprint(cfg: dict[str, Any]) -> str:
        return json.dumps(cfg, sort_keys=True, default=str)

    def get(self, name: str, cfg: dict[str, Any]) -> ProviderAdapter:
        fingerprint = self._fingerprint(cfg)
        hit = self._live.get(name)
        if hit and hit[0] == fingerprint:
            return hit[1]
        if old := self._by_config.get((name, fingerprint)):
            return old  # a request from before the reload
        cls = ADAPTER_TYPES.get(str(cfg.get("type")))
        if cls is None:
            raise UnsupportedProvider(f"provider type {cfg.get('type')!r} ({name}) not supported")
        adapter = cls(name, cfg)
        current = config.registry.providers.get(name)
        if hit and current is not None and self._fingerprint(current) != fingerprint:
            # A stale config whose adapter is already gone: serve this call, don't go live.
            self._retire(adapter, fingerprint)
            return adapter
        if hit:
            self._retire(hit[1], hit[0])
        self._live[name] = (fingerprint, adapter)
        return adapter

    @staticmethod
    def grace_seconds(cfg: dict[str, Any]) -> float:
        """Longer than the slowest request the adapter could still be serving: every
        attempt waiting out its first-token timeout plus backoff, then a full stream."""
        t = cfg.get("timeouts", {})
        retry = config.registry.retry
        per_attempt = float(t.get("first_token", 30)) + retry.backoff_max_ms / 1000
        longest = max(float(t.get("total", 300)), float(t.get("stream_total", 900)))
        return retry.max_attempts_per_provider * per_attempt + longest + 60

    def _retire(self, old: ProviderAdapter, fingerprint: str) -> None:
        self._retired.add(old)
        self._by_config[(old.name, fingerprint)] = old
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): closed at shutdown instead

        async def close_later() -> None:
            await asyncio.sleep(self.grace_seconds(old.cfg))
            if old in self._retired:
                self._retired.discard(old)
                if self._by_config.get((old.name, fingerprint)) is old:
                    del self._by_config[(old.name, fingerprint)]
                await old.aclose()

        task = loop.create_task(close_later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in [a for _, a in self._live.values()] + list(self._retired):
            try:
                await adapter.aclose()
            except Exception:
                log.exception("closing adapter %s failed", adapter.name)
        self._live.clear()
        self._retired.clear()
        self._by_config.clear()


pool = AdapterPool()
