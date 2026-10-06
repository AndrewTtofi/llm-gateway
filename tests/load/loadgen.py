"""Load generator that times every chunk of every stream (Phase 6, ADR 0009).

    LOADGEN_KEY=gw_… python loadgen.py --url http://gateway:8000 --model bench-fast \\
        --stream --rate 50 --duration 30 --out results/x.json

- Open loop (--rate): requests start on a fixed schedule, and latency is measured from
  the *scheduled* time — so if the generator itself falls behind, that queueing delay is
  counted, not hidden (coordinated omission). The scheduling lag is reported too.
- Closed loop (--concurrency): N clients back to back, start times spread over --ramp
  seconds so they don't move in lockstep.
- --warmup: seconds of identical traffic sent first and not recorded.
- --paired: request i's prompt carries `seed=i`; the mock then uses identical delays for
  request i on every run, so a direct run and a gateway run can be compared pairwise.
- A stream only counts as a success if it ends with `[DONE]`.
- Several --url values are used round-robin. The key comes from $LOADGEN_KEY (not argv).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import cycle
from pathlib import Path
from typing import Any

import httpx


@dataclass
class Result:
    t: float  # scheduled start, seconds since measurement began (negative = warm-up)
    status: int | str
    total: float
    seq: int = 0
    ttft: float | None = None
    gaps: list[float] = field(default_factory=list)
    chunks: int = 0
    done: bool = False  # saw [DONE] (streams)
    provider: str = ""
    attempts: str = ""
    admit_ms: float | None = None  # gateway's Server-Timing "admit" duration
    sched_lag: float = 0.0  # how late the request actually started (open loop)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


def pct(xs: list[float], p: float) -> float | None:
    """Nearest-rank percentile."""
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def dist(xs: list[float]) -> dict[str, float | None]:
    return {
        f"p{p}": (round(v * 1000, 2) if (v := pct(xs, p)) is not None else None)
        for p in (50, 95, 99)
    } | {"max": round(max(xs) * 1000, 2) if xs else None}


def summary(results: list[Result], window: float) -> dict[str, Any]:
    ok = [r for r in results if r.ok]
    return {
        "requests": len(results),
        "ok": len(ok),
        "success_rate": round(len(ok) / len(results), 4) if results else None,
        "achieved_rps": round(len(results) / window, 1) if window else None,
        "status": dict(
            Counter(str(r.status) + (f":{r.error}" if r.error else "") for r in results)
        ),
        "served_by": dict(Counter(r.provider for r in ok)),
        "total_ms": dist([r.total for r in ok]),  # successful requests
        "all_ms": dist([r.total for r in results]),  # every response, incl. 429s/errors
        "ttft_ms": dist([r.ttft for r in ok if r.ttft is not None]),
        "gap_ms": dist([g for r in ok for g in r.gaps]),
        "mean_chunks": round(statistics.mean(r.chunks for r in ok), 1) if ok else None,
        "admit_ms": dist([r.admit_ms / 1000 for r in ok if r.admit_ms is not None]),
        "sched_lag_ms": dist([r.sched_lag for r in results]),
    }


async def one(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    scheduled: float,
    t0: float,
    seq: int,
) -> Result:
    start = time.perf_counter()
    res = Result(
        t=scheduled - t0, status=0, total=0.0, seq=seq, sched_lag=max(0.0, start - scheduled)
    )
    try:
        if not body.get("stream"):
            r = await client.post(f"{url}/v1/chat/completions", json=body, headers=headers)
            res.status = r.status_code
            res.done = True
        else:
            async with client.stream(
                "POST", f"{url}/v1/chat/completions", json=body, headers=headers
            ) as r:
                res.status = r.status_code
                last: float | None = None
                async for line in r.aiter_lines():
                    if line == "data: [DONE]":
                        res.done = True
                        break
                    if not line.startswith("data: {"):
                        continue
                    data = json.loads(line[6:])
                    if "error" in data:
                        res.error = "in_band"
                        break
                    choices = data.get("choices") or []
                    if choices and choices[0].get("delta", {}).get("content"):
                        now = time.perf_counter()
                        if res.ttft is None:
                            res.ttft = now - scheduled
                        elif last is not None:
                            res.gaps.append(now - last)
                        last = now
                        res.chunks += 1
            if res.status == 200 and not res.error and not res.done:
                res.error = "truncated"  # ended without [DONE]
        res.provider = r.headers.get("x-gateway-provider", "")
        res.attempts = r.headers.get("x-gateway-attempts", "")
        timing = r.headers.get("server-timing", "")
        if "admit;dur=" in timing:
            res.admit_ms = float(timing.split("admit;dur=")[1].split(",")[0])
    except Exception as exc:  # timeouts, refused connections: part of the result
        res.status, res.error = "exc", type(exc).__name__
    res.total = time.perf_counter() - scheduled
    return res


def make_body(args: argparse.Namespace, seq: int) -> dict[str, Any]:
    prompt = args.prompt
    if args.prompt_chars > len(prompt):  # a long context, like a chat resent in full
        filler = " The river carried silt past the old mill and on towards the sea."
        prompt = (prompt + filler * (args.prompt_chars // len(filler) + 1))[: args.prompt_chars]
    prompt = f"{prompt} seed={seq}" if args.paired else prompt
    body: dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if args.stream:
        body["stream"] = True
    return body


async def run(args: argparse.Namespace) -> dict[str, Any]:
    key = os.environ.get("LOADGEN_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    urls = cycle(args.url)
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=2000)
    timeout = httpx.Timeout(args.timeout, connect=10)
    results: list[Result] = []
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        t0 = time.perf_counter() + args.warmup  # measurement starts after the warm-up
        started_at = time.time() + args.warmup  # wall clock of t=0, to line up faults
        if args.rate:
            interval = 1 / args.rate
            tasks = []
            seq, at = 0, time.perf_counter()
            while at < t0 + args.duration:
                await asyncio.sleep(max(0.0, at - time.perf_counter()))
                tasks.append(
                    asyncio.create_task(
                        one(client, next(urls), headers, make_body(args, seq), at, t0, seq)
                    )
                )
                seq += 1
                at += interval
            results = list(await asyncio.gather(*tasks))
        else:
            counter = iter(range(10**9))

            async def worker() -> None:
                await asyncio.sleep(random.uniform(0, args.ramp))  # noqa: S311
                while time.perf_counter() < t0 + args.duration:
                    seq = next(counter)
                    results.append(
                        await one(
                            client,
                            next(urls),
                            headers,
                            make_body(args, seq),
                            time.perf_counter(),
                            t0,
                            seq,
                        )
                    )

            await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    measured = [r for r in results if r.t >= 0]  # drop warm-up
    out: dict[str, Any] = {
        "started_at": started_at,
        "label": args.label,
        "config": vars(args),
        "summary": summary(measured, args.duration),
    }
    if args.timeline:
        out["requests"] = [asdict(r) | {"gaps": None} for r in measured]
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", action="append", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--stream", action="store_true")
    p.add_argument("--rate", type=float, default=0, help="open loop: requests per second")
    p.add_argument("--concurrency", type=int, default=10, help="closed loop clients")
    p.add_argument("--ramp", type=float, default=0, help="closed loop: spread client starts")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--warmup", type=float, default=0, help="seconds sent first, not recorded")
    p.add_argument("--paired", action="store_true", help="seed=i in prompt i (mock timing)")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--prompt", default="Write a few sentences about rivers.")
    p.add_argument("--prompt-chars", type=int, default=0, help="pad the prompt to N characters")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--label", default="")
    p.add_argument("--timeline", action="store_true", help="keep every request in the output")
    p.add_argument("--out", default="")
    args = p.parse_args()
    out = asyncio.run(run(args))
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(json.dumps(out["summary"]))


if __name__ == "__main__":
    main()
