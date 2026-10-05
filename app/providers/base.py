"""Interface every provider adapter implements. Internal format = OpenAI chat-completions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

# Upstream statuses worth retrying or falling back on (used from Phase 3).
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
# Upstream 4xx caused by the client's own request. The provider's message only describes
# the client's input and helps them fix it, so it is passed through.
CLIENT_FAULT_STATUS = frozenset({400, 413, 422})


class ProviderError(Exception):
    """An upstream call failed. `status` is the upstream HTTP status, None for network errors.

    `message` is safe to show clients. `detail` is the provider's raw error text — it can
    contain key fragments, org/project IDs or internal hosts, so it must never be returned
    to clients (log a hash of it at most).
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retryable: bool = False,
        timeout: bool = False,
        headers: dict[str, str] | None = None,
        code: str | None = None,
        detail: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.timeout = timeout
        self.headers = headers or {}
        self.code = code  # provider's machine-readable code, e.g. "insufficient_quota"
        self.detail = detail


class ProviderAdapter(ABC):
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming call. Takes and returns OpenAI-format dicts."""

    @abstractmethod
    def stream(self, model: str, request: dict[str, Any]) -> AsyncGenerator[dict[str, Any]]:
        """Streaming call. Yields OpenAI-format chunk dicts.

        Closing the iterator (aclose / cancellation) must close the upstream connection.
        """

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        """Release connections. Called on shutdown and when config replaces the adapter."""
        return None
