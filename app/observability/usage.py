"""Usage records: built per request, written to Postgres in the background (ADR 0008)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import UsageRow
from app.observability import metrics

log = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    created_at: datetime
    request_id: str
    key_id: str
    key_prefix: str
    alias: str
    target: str | None
    status: int
    error_code: str | None
    streamed: bool
    fallback: bool
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    usage_estimated: bool
    cost_usd: float | None
    latency_ms: int
    ttft_ms: int | None
    estimated_tokens: int | None = None  # what the limiter reserved before the call
    team: str | None = None

    def row(self) -> dict[str, Any]:
        out = asdict(self)
        # Columns are bounded; one oversized value must never sink a whole batch.
        for col, limit in (
            ("request_id", 64),
            ("key_prefix", 16),
            ("alias", 200),
            ("target", 200),
            ("error_code", 100),
            ("team", 100),
        ):
            if isinstance(out[col], str):
                out[col] = out[col][:limit]
        provider, _, model = (self.target or "").partition("/")
        out["provider"], out["model"] = provider or None, model or None
        try:
            out["key_id"] = uuid.UUID(self.key_id)
        except ValueError:  # in-memory keys (tests) have non-UUID ids
            out["key_id"] = None
        return out


class UsageSink(Protocol):
    def submit(self, record: UsageRecord) -> None: ...


class MemoryUsageSink:
    """Keeps records in a list — tests and `GATEWAY_STORES=memory`."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def submit(self, record: UsageRecord) -> None:
        self.records.append(record)


class PostgresUsageWriter:
    """Queue + one background task that inserts batches. `submit` never blocks or raises:
    if the queue is full (Postgres slow or down), the row is dropped and counted."""

    def __init__(
        self,
        sessions: async_sessionmaker[Any],
        max_queue: int = 10_000,
        batch_size: int = 100,
        flush_seconds: float = 1.0,
    ) -> None:
        self.sessions = sessions
        self.batch_size, self.flush_seconds = batch_size, flush_seconds
        # None is the stop sentinel: the writer finishes its current batch, then exits.
        self.queue: asyncio.Queue[UsageRecord | None] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None

    def submit(self, record: UsageRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            metrics.usage_dropped.inc()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _take_batch(self) -> tuple[list[UsageRecord], bool]:
        """Up to batch_size rows or flush_seconds, whichever first. → (rows, stop?)"""
        first = await self.queue.get()
        if first is None:
            return [], True
        batch = [first]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.flush_seconds
        while len(batch) < self.batch_size:
            timeout = deadline - loop.time()
            if timeout <= 0:
                break
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                break
            if item is None:
                return batch, True
            batch.append(item)
        return batch, False

    async def _insert(self, rows: list[dict[str, Any]]) -> None:
        async with self.sessions() as s, s.begin():
            await s.execute(insert(UsageRow), rows)

    async def _write(self, batch: list[UsageRecord]) -> None:
        rows = [r.row() for r in batch]
        try:
            await self._insert(rows)
            return
        except Exception as exc:
            if len(rows) == 1:
                metrics.usage_dropped.inc()
                log.error("usage row dropped: %s", type(exc).__name__)
                return
        # The batch failed. Maybe one bad row, maybe Postgres is down: try the rows one by
        # one, and stop after a few consecutive failures (then it's the database).
        failures = 0
        for i, row in enumerate(rows):
            try:
                await self._insert([row])
                failures = 0
            except Exception as exc:
                metrics.usage_dropped.inc()
                failures += 1
                if failures >= self.MAX_CONSECUTIVE_FAILURES:
                    metrics.usage_dropped.inc(len(rows) - i - 1)
                    log.error(
                        "usage log write failed (%d rows dropped): %s",
                        len(rows) - i - 1 + failures,
                        type(exc).__name__,
                    )
                    return
                log.error("usage row rejected: %s", type(exc).__name__)

    async def _run(self) -> None:
        while True:
            batch, stop = await self._take_batch()
            if batch:
                await self._write(batch)
            if stop:
                return

    STOP_SECONDS = 10.0  # how long shutdown waits for the last batch
    MAX_CONSECUTIVE_FAILURES = 3  # per-row retries before deciding the database is down

    async def stop(self) -> None:
        """Finish the batch in progress and everything queued, then stop."""
        if self._task is not None and not self._task.done():
            await self.queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(self._task), self.STOP_SECONDS)
            except TimeoutError:
                self._task.cancel()
        self._task = None
        pending = [r for r in iter_nowait(self.queue) if r is not None]
        for i in range(0, len(pending), self.batch_size):
            await self._write(pending[i : i + self.batch_size])


def iter_nowait(queue: asyncio.Queue[UsageRecord | None]) -> list[UsageRecord | None]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items
