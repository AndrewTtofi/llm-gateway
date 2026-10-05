"""PostgresUsageWriter against a real Postgres (migration 0002)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.db import make_engine, make_sessions
from app.observability import metrics
from app.observability.usage import PostgresUsageWriter, UsageRecord
from tests.test_keys_postgres import URL, store  # noqa: F401 — migrates the test DB


def rec(i: int, **extra: Any) -> UsageRecord:
    base: dict[str, Any] = dict(
        created_at=datetime.now(UTC),
        request_id=f"r{i}",
        key_id="not-a-uuid",
        key_prefix="gw_test",
        alias="fast",
        target="anthropic/claude-x",
        status=200,
        error_code=None,
        streamed=False,
        fallback=False,
        attempts=1,
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        usage_estimated=False,
        cost_usd=0.001,
        latency_ms=12,
        ttft_ms=None,
    )
    return UsageRecord(**{**base, **extra})


@pytest.fixture
async def writer(store: Any) -> AsyncIterator[PostgresUsageWriter]:  # noqa: F811
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_log"))
    w = PostgresUsageWriter(make_sessions(engine), batch_size=10, flush_seconds=0.05)
    yield w
    await engine.dispose()


async def count(w: PostgresUsageWriter) -> int:
    async with w.sessions() as s:
        return int(await s.scalar(text("SELECT count(*) FROM usage_log")) or 0)


async def test_rows_are_written_in_batches(writer: PostgresUsageWriter) -> None:
    writer.start()
    for i in range(25):
        writer.submit(rec(i))
    for _ in range(50):
        if await count(writer) == 25:
            break
        await asyncio.sleep(0.05)
    await writer.stop()
    assert await count(writer) == 25
    async with writer.sessions() as s:
        row = (
            (await s.execute(text("SELECT * FROM usage_log WHERE request_id='r0'")))
            .mappings()
            .one()
        )
    assert row["provider"] == "anthropic" and row["model"] == "claude-x"


async def test_stop_flushes_what_is_queued(writer: PostgresUsageWriter) -> None:
    for i in range(7):
        writer.submit(rec(i))  # never started: only the shutdown flush writes them
    await writer.stop()
    assert await count(writer) == 7


async def test_full_queue_drops_and_counts_instead_of_blocking(store: Any) -> None:  # noqa: F811
    w = PostgresUsageWriter(make_sessions(make_engine(URL)), max_queue=3)
    before = metrics.registry.get_sample_value("gateway_usage_log_dropped_total") or 0
    for i in range(5):
        w.submit(rec(i))  # returns immediately even though nothing drains the queue
    after = metrics.registry.get_sample_value("gateway_usage_log_dropped_total") or 0
    assert after - before == 2


async def test_database_down_drops_rows_without_raising() -> None:
    dead = make_engine("postgresql+asyncpg://x:y@127.0.0.1:1/none", timeout=0.2)
    w = PostgresUsageWriter(make_sessions(dead))
    before = metrics.registry.get_sample_value("gateway_usage_log_dropped_total") or 0
    w.submit(rec(1))
    await w.stop()
    after = metrics.registry.get_sample_value("gateway_usage_log_dropped_total") or 0
    assert after - before == 1
    await dead.dispose()


async def test_stop_finishes_the_batch_in_progress(writer: PostgresUsageWriter) -> None:
    writer.flush_seconds = 5  # the writer is mid-batch, waiting for more rows
    writer.start()
    for i in range(5):
        writer.submit(rec(i))
    await asyncio.sleep(0.05)
    await writer.stop()
    assert await count(writer) == 5


async def test_one_bad_row_doesnt_sink_the_batch(writer: PostgresUsageWriter) -> None:
    writer.submit(rec(1))
    writer.submit(rec(2, attempts=2**40))  # out of int range: Postgres rejects this row
    writer.submit(rec(3))
    await writer.stop()
    assert await count(writer) == 2


async def test_long_values_are_truncated_not_rejected(writer: PostgresUsageWriter) -> None:
    writer.submit(rec(1, alias="a" * 500, error_code="e" * 500))
    await writer.stop()
    assert await count(writer) == 1
