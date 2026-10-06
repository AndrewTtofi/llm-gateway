"""LLM-as-judge sampling (ADR 0022)."""

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import judge, services
from app.config import JudgeConfig, Registry
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION, chunk, sse

URL = f"{UPSTREAM}/chat/completions"
MSGS = [{"role": "user", "content": "What is 2+2?"}]


class Recorder:
    def __init__(self) -> None:
        self.jobs: list[judge.Job] = []

    def submit(self, job: judge.Job) -> None:
        self.jobs.append(job)

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.fixture
def sampled(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(
        registry.aliases["local"], "judge", JudgeConfig(sample_rate=1.0, judge="local")
    )
    recorder = Recorder()
    monkeypatch.setattr(services, "judge", recorder)
    return recorder


def answer(text: str) -> dict[str, Any]:
    return {
        **COMPLETION,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"score": 4, "labels": ["good"]}', (4, ["good"])),
        (
            'Sure! {"score": 2, "labels": ["incorrect", "verbose", "made_up"]}',
            (2, ["incorrect", "verbose"]),
        ),
        ('{"score": 7}', None),
        ('{"score": "5"}', None),
        ('{"score": true}', None),
        ("I think it's a 4", None),
        ("", None),
    ],
)
def test_parse_accepts_only_valid_scores_and_known_labels(text: str, expected: Any) -> None:
    assert judge.parse(text) == expected


@respx.mock
def test_sampled_answers_are_submitted_with_their_text(
    client: TestClient, sampled: Recorder
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=answer("4")))
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    [job] = sampled.jobs
    assert job.alias == "local" and job.target == "mock/tiny"
    assert job.conversation == "user: What is 2+2?" and job.answer == "4"


@respx.mock
def test_streamed_answers_are_assembled_for_judging(client: TestClient, sampled: Recorder) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, content=sse(chunk("fo"), chunk("ur"), chunk(finish="stop"), "[DONE]")
        )
    )
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "local", "messages": MSGS, "stream": True}
    ) as r:
        list(r.iter_lines())
    assert [j.answer for j in sampled.jobs] == ["four"]


@respx.mock
def test_failed_answers_are_not_judged(client: TestClient, sampled: Recorder) -> None:
    respx.post(URL).mock(return_value=httpx.Response(500, json={"error": {"message": "x"}}))
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert sampled.jobs == []


@respx.mock
def test_unsampled_requests_keep_nothing(
    client: TestClient, sampled: Recorder, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registry.aliases["local"], "judge", JudgeConfig(sample_rate=0.0001, judge="local")
    )
    monkeypatch.setattr(judge, "sampled", lambda cfg: False)
    respx.post(URL).mock(return_value=httpx.Response(200, json=answer("4")))
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert sampled.jobs == []


@respx.mock
async def test_evaluate_stores_score_and_labels_only(registry: Registry) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json=answer('{"score": 5, "labels": ["good"]}'))
    )
    store = judge.MemoryScoreStore()
    j = judge.Judge(store)
    job = judge.Job(
        "req-1",
        "local",
        "mock/tiny",
        "control",
        JudgeConfig(sample_rate=1, judge="local"),
        "user: hi",
        "hello",
    )
    score = await j.evaluate(job)
    assert score is not None and score.score == 5 and score.labels == ["good"]
    assert score.judge_target == "mock/tiny"
    assert store.scores == [score]
    stored = vars(score)
    assert "hello" not in json.dumps(stored, default=str)  # no content stored
    sent = json.loads(route.calls.last.request.content)
    assert "<answer>\nhello\n</answer>" in sent["messages"][1]["content"]


@respx.mock
async def test_unusable_verdicts_are_counted_not_stored(registry: Registry) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=answer("great answer!")))
    store = judge.MemoryScoreStore()
    job = judge.Job("r", "local", None, None, JudgeConfig(sample_rate=1, judge="local"), "c", "a")
    assert await judge.Judge(store).evaluate(job) is None and store.scores == []


async def test_queue_is_bounded_and_drops() -> None:
    from app.observability import metrics

    j = judge.Judge(judge.MemoryScoreStore(), maxsize=2)
    job = judge.Job("r", "drops", None, None, JudgeConfig(sample_rate=1, judge="x"), "c", "a")
    before = (
        metrics.registry.get_sample_value(
            "gateway_judge_total", {"alias": "drops", "result": "dropped"}
        )
        or 0
    )
    j.submit(job)  # not started: dropped
    j.queue = __import__("asyncio").Queue(maxsize=2)
    for _ in range(3):
        j.submit(job)
    after = metrics.registry.get_sample_value(
        "gateway_judge_total", {"alias": "drops", "result": "dropped"}
    )
    assert after == before + 2 and j.queue.qsize() == 2


async def test_postgres_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    from app.db import make_engine, make_sessions
    from tests.test_keys_postgres import URL as PG
    from tests.test_keys_postgres import ensure_database

    try:
        await ensure_database()
    except Exception:
        import os

        if os.environ.get("REQUIRE_POSTGRES"):
            raise
        pytest.skip("no Postgres")
    engine = make_engine(PG)
    async with engine.begin() as conn:
        from alembic import command
        from alembic.config import Config

        def upgrade(sync_conn: object) -> None:
            cfg = Config("alembic.ini")
            cfg.attributes["connection"] = sync_conn
            command.upgrade(cfg, "head")

        await conn.run_sync(upgrade)
        await conn.execute(text("TRUNCATE judge_scores"))
    store = judge.PostgresScoreStore(make_sessions(engine))
    await store.save(
        judge.Score("req-9", "local", "mock/tiny", None, "mock/tiny", 3, ["incomplete"])
    )
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT request_id, score, labels FROM judge_scores"))).one()
    await engine.dispose()
    assert row.request_id == "req-9" and row.score == 3 and row.labels == ["incomplete"]


def test_judge_and_semantic_cache_references_are_checked_on_load(registry: Registry) -> None:
    from app.config import Registry as R

    data = registry.model_dump()
    data["aliases"]["local"]["judge"] = {"sample_rate": 0.5, "judge": "nonexistent"}
    with pytest.raises(ValueError):
        R.model_validate(data)
    data["aliases"]["local"]["judge"] = None
    data["aliases"]["local"]["cache"] = {"mode": "semantic"}  # no embedding model
    with pytest.raises(ValueError):
        R.model_validate(data)


def test_submitted_text_is_truncated() -> None:
    j = judge.Judge(judge.MemoryScoreStore())
    job = judge.Job(
        "r",
        "a",
        None,
        None,
        JudgeConfig(sample_rate=1, judge="x", max_chars=1000),
        "c" * 10_000,
        "a" * 10_000,
    )
    j.submit(job)
    assert len(job.conversation) == 500 and len(job.answer) == 500
