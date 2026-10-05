"""Interface every provider adapter implements. Internal format = OpenAI chat-completions."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
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


class UnsupportedRequest(ValueError):
    """The request uses something this provider can't express → 400 for the client."""


# Provider codes meaning "out of credit": retrying won't help, so it's not a 429 for clients.
QUOTA_CODES = frozenset({"insufficient_quota", "billing_error"})


def upstream_status_error(
    provider: str,
    status: int,
    detail: str,
    code: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> ProviderError:
    """Build a ProviderError whose `message` is safe to show clients (see ADR 0002)."""
    if status == 402:
        code = code or "billing_error"
    if status in CLIENT_FAULT_STATUS:
        message = f"{provider} rejected the request: {detail}"
    elif status in (401, 403):
        message = f"{provider} rejected the gateway's credentials (gateway configuration)"
    elif status == 404:
        message = (
            f"{provider} does not know the configured model or endpoint (gateway configuration)"
        )
    elif code in QUOTA_CODES:
        message = f"{provider} quota exhausted"
    elif status == 429:
        message = f"{provider} rate limited the gateway"
    else:
        message = f"{provider} returned HTTP {status}"
    keep = {k: v for k, v in (headers or {}).items() if k.lower() == "retry-after"}
    return ProviderError(
        message,
        status=status,
        retryable=status in RETRYABLE_STATUS and code not in QUOTA_CODES,
        headers=keep,
        code=code,
        detail=detail[:500],
    )


async def first_then_rest[T](
    events: AsyncIterator[T], first_timeout: float, provider: str
) -> AsyncGenerator[T]:
    """Yield `events`, allowing `first_timeout` for the first one only.

    The wait for the first event (prompt processing, a declined refusal attempt) has its
    own budget; gaps after that are bounded by the HTTP read timeout (`stream_idle`).
    """
    try:
        async with asyncio.timeout(first_timeout):
            first = await anext(events)
    except StopAsyncIteration:
        return
    except TimeoutError as exc:
        raise ProviderError(
            f"{provider} timed out waiting for the first token", retryable=True, timeout=True
        ) from exc
    yield first
    async for event in events:
        yield event


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
