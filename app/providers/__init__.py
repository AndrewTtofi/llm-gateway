"""Builds and caches one adapter (and its HTTP connection pool) per configured provider."""

from __future__ import annotations

import json
from typing import Any

from app.providers.anthropic import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.openai_compat import OpenAICompatAdapter

ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAICompatAdapter,
    "anthropic": AnthropicAdapter,
}


class UnsupportedProvider(Exception):
    pass


class AdapterPool:
    """Adapters are reused across requests so connections stay warm.

    On config reload a provider whose config changed gets a fresh adapter; the old one
    may still be serving in-flight requests, so it is only closed at shutdown.
    """

    def __init__(self) -> None:
        self._live: dict[str, tuple[str, ProviderAdapter]] = {}
        self._retired: list[ProviderAdapter] = []

    def get(self, name: str, cfg: dict[str, Any]) -> ProviderAdapter:
        fingerprint = json.dumps(cfg, sort_keys=True, default=str)
        hit = self._live.get(name)
        if hit and hit[0] == fingerprint:
            return hit[1]
        cls = ADAPTER_TYPES.get(str(cfg.get("type")))
        if cls is None:
            raise UnsupportedProvider(f"provider type {cfg.get('type')!r} ({name}) not supported")
        adapter = cls(name, cfg)
        if hit:
            self._retired.append(hit[1])
        self._live[name] = (fingerprint, adapter)
        return adapter

    async def aclose(self) -> None:
        for adapter in [a for _, a in self._live.values()] + self._retired:
            await adapter.aclose()
        self._live.clear()
        self._retired.clear()


pool = AdapterPool()
