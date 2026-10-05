"""Dependency probes behind /readyz: statuses, and a bound on how long they can take."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app import services


class FakeRedis:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def ping(self) -> bool:
        if self.behaviour == "error":
            raise ConnectionError("refused")
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        return True


class FakeConn:
    async def execute(self, _: Any) -> None:
        return None


class FakeEngine:
    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeConn]:
        if self.behaviour == "error":
            raise OSError("refused")
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        yield FakeConn()


@pytest.mark.parametrize(
    ("redis", "postgres", "expected"),
    [
        ("ok", "ok", {"redis": "ok", "postgres": "ok"}),
        ("error", "ok", {"redis": "unreachable", "postgres": "ok"}),
        ("ok", "error", {"redis": "ok", "postgres": "unreachable"}),
        ("hang", "hang", {"redis": "unreachable", "postgres": "unreachable"}),
    ],
)
async def test_check_dependencies_statuses(
    monkeypatch: pytest.MonkeyPatch, redis: str, postgres: str, expected: dict[str, str]
) -> None:
    monkeypatch.setattr(services, "_redis", FakeRedis(redis))
    monkeypatch.setattr(services, "_engine", FakeEngine(postgres))
    assert await services.check_dependencies(limit_s=0.05) == expected


async def test_hung_dependencies_are_probed_concurrently_within_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "_redis", FakeRedis("hang"))
    monkeypatch.setattr(services, "_engine", FakeEngine("hang"))
    t0 = time.perf_counter()
    await services.check_dependencies(limit_s=0.2)
    assert time.perf_counter() - t0 < 0.35  # one timeout, not one per dependency


async def test_no_dependencies_in_memory_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_redis", None)
    monkeypatch.setattr(services, "_engine", None)
    assert await services.check_dependencies() == {}


def test_readyz_reads_the_cache_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_deps", {"redis": "ok", "postgres": "unreachable"})
    deps = services.dependencies()
    assert deps == {"redis": "ok", "postgres": "unreachable"}
    deps["redis"] = "changed"
    assert services.dependencies()["redis"] == "ok"  # a copy, not the cache itself
