"""Mock OpenAI-compatible LLM for benchmarks (Phase 6). Not part of the gateway.

The model name picks a timing profile, so the same request can go *directly* here or
*through the gateway* and the difference is the gateway's overhead.

- "realistic" samples TTFT and inter-chunk gaps from the empirical distributions measured
  on real Claude Haiku streams (`real_claude_profile.json`: inverse CDFs).
- A prompt containing `seed=<n>` makes that request's delays deterministic, so a direct
  run and a gateway run with the same seeds see *identical* provider timing and can be
  compared pairwise — removing the mock's own randomness from the comparison.
- Completion length honours `max_tokens` (like a real provider), so token and cost
  accounting can be checked exactly.

    POST /v1/chat/completions      OpenAI chat completions, streaming or not
    POST /control                  {"fail": ["primary"]}  → those models return 503
    GET  /stats                    requests served per model
"""

from __future__ import annotations

import asyncio
import bisect
import json
import random
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

REAL = json.loads((Path(__file__).parent / "real_claude_profile.json").read_text())
TOKENS_PER_CHUNK = float(REAL["tokens_per_chunk"])


def empirical(quantiles: list[float]) -> Callable[[random.Random], float]:
    """Sampler from an inverse CDF given at 0%, 1%, …, 100% (linear in between)."""
    steps = [i / 100 for i in range(101)]

    def draw(rng: random.Random) -> float:
        u = rng.random()
        i = min(bisect.bisect_right(steps, u), 100)
        lo, hi = quantiles[i - 1], quantiles[i]
        return lo + (hi - lo) * (u - steps[i - 1]) * 100

    return draw


def uniform(lo: float, hi: float) -> Callable[[random.Random], float]:
    return lambda rng: rng.uniform(lo, hi)


# ttft / gap: samplers (seconds). chunks: content chunks before max_tokens caps them.
PROFILES: dict[str, dict[str, Any]] = {
    "realistic": {
        "ttft": empirical(REAL["ttft_quantiles_s"]),
        "gap": empirical(REAL["gap_quantiles_s"]),
        "chunks": round(REAL["chunks_per_response_mean"]),
    },
    # near-zero provider latency, so the gateway's own overhead is what's left
    "fast": {"ttft": uniform(0, 0), "gap": uniform(0, 0), "chunks": 20},
    # long streams (~5 s); ±10% jitter so concurrent clients don't move in lockstep
    "long": {"ttft": uniform(0.27, 0.33), "gap": uniform(0.0225, 0.0275), "chunks": 200},
    # slower than the gateway's first-token timeout for the bench provider
    "slow": {"ttft": uniform(30, 30), "gap": uniform(0, 0), "chunks": 5},
    # breaker scenario: a primary that can be told to fail, and a backup
    "primary": {"ttft": uniform(0.01, 0.01), "gap": uniform(0, 0), "chunks": 10},
    "backup": {"ttft": uniform(0.01, 0.01), "gap": uniform(0, 0), "chunks": 10},
}
SEED = re.compile(r"seed=(\d+)")

app = FastAPI(title="mock LLM")
failing: set[str] = set()
served: Counter[str] = Counter()


def _prompt(body: dict[str, Any]) -> str:
    return " ".join(str(m.get("content") or "") for m in body.get("messages", []))


def _usage(prompt: str, chunks: int) -> dict[str, int]:
    p, c = max(1, len(prompt) // 4), round(chunks * TOKENS_PER_CHUNK)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


@app.post("/control")
async def control(body: dict[str, Any]) -> dict[str, Any]:
    failing.clear()
    failing.update(body.get("fail", []))
    return {"failing": sorted(failing)}


@app.get("/stats")
async def stats() -> dict[str, int]:
    return dict(served)


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> JSONResponse | StreamingResponse:
    body = await request.json()
    model = str(body.get("model", "fast"))
    profile = PROFILES.get(model, PROFILES["fast"])
    if model in failing:
        return JSONResponse(
            {"error": {"message": "mock outage", "type": "api_error"}}, status_code=503
        )
    served[model] += 1
    prompt = _prompt(body)
    seed = SEED.search(prompt)
    rng = random.Random(int(seed.group(1)) if seed else None)  # noqa: S311 — timing only
    max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or 10**6)
    chunks = max(1, min(profile["chunks"], int(max_tokens / TOKENS_PER_CHUNK)))
    ttft = profile["ttft"](rng)
    gaps = [profile["gap"](rng) for _ in range(chunks - 1)]
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())

    if not body.get("stream"):
        await asyncio.sleep(ttft + sum(gaps))
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "word " * chunks},
                    }
                ],
                "usage": _usage(prompt, chunks),
            }
        )

    async def events() -> AsyncIterator[str]:
        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            return (
                "data: "
                + json.dumps(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                )
                + "\n\n"
            )

        await asyncio.sleep(ttft)
        yield chunk({"role": "assistant", "content": ""})
        for i in range(chunks):
            if i:
                await asyncio.sleep(gaps[i - 1])
            yield chunk({"content": "word "})
        yield chunk({}, "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": _usage(prompt, chunks),
                    }
                )
                + "\n\n"
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
