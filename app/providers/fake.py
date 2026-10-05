"""Chaos provider: answers like an OpenAI-compatible model, fails on purpose.

Each configured "model" is a failure profile (see `providers.fake.models` in
`config/models.yaml`). Used by tests, the chaos aliases and the Phase 6 load tests to
prove that retries, fallback and circuit breakers behave — without paying for tokens.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from app.providers.base import ProviderAdapter, ProviderError, upstream_status_error


class FakeAdapter(ProviderAdapter):
    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        # Not for security — chaos only. A seed makes test runs reproducible.
        self._rng = random.Random(cfg.get("seed"))  # noqa: S311

    def _profile(self, model: str) -> dict[str, Any]:
        profiles: dict[str, Any] = self.cfg.get("models", {})
        if model not in profiles:
            raise upstream_status_error(self.name, 404, f"no fake profile {model!r}")
        profile: dict[str, Any] = profiles[model]
        return profile

    async def _misbehave(self, profile: dict[str, Any]) -> None:
        lo, hi = profile.get("latency_ms", [0, 0])
        if hi:
            await asyncio.sleep(self._rng.uniform(lo, hi) / 1000)
        if self._is_down(profile) or self._rng.random() < float(profile.get("failure_rate", 0)):
            status = int(self._rng.choice(profile.get("fail_status") or [500]))
            raise upstream_status_error(self.name, status, "chaos: injected failure")

    @staticmethod
    def _is_down(profile: dict[str, Any]) -> bool:
        window = profile.get("down_window")
        if not window:
            return False
        return (time.time() % float(window["period_s"])) < float(window["down_s"])

    @staticmethod
    def _reply(model: str) -> str:
        return f"Hello from fake/{model}."

    @staticmethod
    def _usage(request: dict[str, Any], completion: int) -> dict[str, int]:
        chars = sum(len(str(m.get("content") or "")) for m in request.get("messages", []))
        prompt = max(1, chars // 4)  # ~4 chars per token, close enough for a fake
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile(model)
        await self._misbehave(profile)
        text = self._reply(model)
        return {
            "id": f"chatcmpl-fake-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": self._usage(request, len(text.split())),
        }

    async def stream(self, model: str, request: dict[str, Any]) -> AsyncGenerator[dict[str, Any]]:
        profile = self._profile(model)
        await self._misbehave(profile)
        cid, created = f"chatcmpl-fake-{uuid.uuid4().hex[:12]}", int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        words = self._reply(model).split()
        yield chunk({"role": "assistant", "content": ""})
        for i, word in enumerate(words):
            if i == 1 and self._rng.random() < float(profile.get("mid_stream_failure_rate", 0)):
                raise ProviderError(
                    f"{self.name} failed mid-stream",
                    retryable=True,
                    detail="chaos: injected mid-stream failure",
                )
            await asyncio.sleep(float(profile.get("chunk_delay_ms", 0)) / 1000)
            yield chunk({"content": (" " if i else "") + word})
        yield chunk({}, "stop")
        if (request.get("stream_options") or {}).get("include_usage"):
            final = chunk({})
            final["choices"] = []
            final["usage"] = self._usage(request, len(words))
            yield final
