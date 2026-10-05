"""Interface every provider adapter implements. Internal format = OpenAI chat-completions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ProviderAdapter(ABC):
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming call. Takes and returns OpenAI-format dicts."""

    @abstractmethod
    def stream(self, model: str, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Streaming call. Yields OpenAI-format chunk dicts."""

    async def health(self) -> bool:
        return True
