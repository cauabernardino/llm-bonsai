"""Closed-loop load test against the gateway: N workers, each streaming one request
after another for a fixed duration.

Reports latency, TTFT (time to the first chunk with non-empty delta.content) and the
error rate per concurrency level. Run a sweep to watch requests queue once concurrency
passes what the model server runs in parallel (OLLAMA_NUM_PARALLEL=2 here).
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import httpx

import lab

PROMPT = "Explain in three sentences why the sky is blue."
# A failing request returns instantly; pause so a dead gateway doesn't become a hot loop.
ERROR_BACKOFF_S = 0.25


@dataclass
class Result:
    started: float  # seconds since the run began
    latency: float
    ttft: float | None
    error: str | None


async def one_request(client: httpx.AsyncClient, model: str, max_tokens: int) -> tuple[float | None, str | None]:
    """Stream one completion; returns (ttft, error)."""
    body = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    start = time.perf_counter()
    ttft = None
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        if response.status_code != 200:
            await response.aread()
            return None, f"HTTP {response.status_code}"
        async for line in response.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            choices = json.loads(line[len("data: "):]).get("choices") or [{}]
            if ttft is None and choices[0].get("delta", {}).get("content"):
                ttft = time.perf_counter() - start
    return ttft, None


async def run(
    concurrency: int, duration: float, model: str = "chat-small", key: str | None = None, max_tokens: int = 64
) -> list[Result]:
    key = key or lab.env("LITELLM_KEY_SUPPORT_BOT")
    results: list[Result] = []
    t0 = time.perf_counter()

    async def worker(client: httpx.AsyncClient) -> None:
        while (begin := time.perf_counter()) - t0 < duration:
            try:
                ttft, error = await one_request(client, model, max_tokens)
            except httpx.HTTPError as exc:
                ttft, error = None, type(exc).__name__
            results.append(Result(begin - t0, time.perf_counter() - begin, ttft, error))
            if error:
                await asyncio.sleep(ERROR_BACKOFF_S)

    async with httpx.AsyncClient(
        base_url=lab.GATEWAY_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(120, connect=5),
    ) as client:
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
    return results


def percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


@dataclass
class Level:
    concurrency: int
    attempts: int
    ok: int
    rps: float
    p50: float | None
    p95: float | None
    ttft50: float | None
    ttft95: float | None
    errors: Counter


def measure(concurrency: int, results: list[Result]) -> Level:
    ok = [r for r in results if r.error is None]
    # Requests in flight at the deadline still finish, so the run lasts longer than
    # --duration; divide by the real wall time or high concurrency looks faster than it is.
    wall = max((r.started + r.latency for r in results), default=1.0)
    latencies = [r.latency for r in ok]
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    return Level(
        concurrency=concurrency,
        attempts=len(results),
        ok=len(ok),
        rps=len(ok) / wall,
        p50=percentile(latencies, 50),
        p95=percentile(latencies, 95),
        ttft50=percentile(ttfts, 50),
        ttft95=percentile(ttfts, 95),
        errors=Counter(r.error for r in results if r.error),
    )


def fmt(seconds: float | None) -> str:
    return f"{seconds:7.2f}s" if seconds is not None else f"{'–':>8}"


HEADER = f"{'conc':>4} {'reqs':>5} {'req/s':>6} {'p50 lat':>8} {'p95 lat':>8} {'p50 ttft':>8} {'p95 ttft':>8}  errors"


def row(level: Level) -> str:
    errors = "0"
    if level.errors:
        kinds = ", ".join(f"{kind} ×{count}" for kind, count in level.errors.most_common())
        errors = f"{sum(level.errors.values())} ({kinds})"
    return (
        f"{level.concurrency:>4} {level.attempts:>5} {level.rps:>6.2f} {fmt(level.p50)} {fmt(level.p95)} "
        f"{fmt(level.ttft50)} {fmt(level.ttft95)}  {errors}"
    )


def explain(levels: list[Level]) -> None:
    by_conc = {level.concurrency: level for level in levels}
    if any(level.ok == 0 for level in levels):
        print("\nSome levels had no successful requests; see the errors column (is `make pf` running?).")
        return
    if any(level.ok < 20 for level in levels):
        print("\n(p95 over fewer than 20 requests is close to the slowest one; use --duration for more.)")

    shown = []
    if 1 in by_conc and 2 in by_conc:
        one, two = by_conc[1], by_conc[2]
        shown.append(
            f"At 2 concurrent requests TTFT stayed low ({one.ttft50:.2f}s → {two.ttft50:.2f}s): Ollama works on "
            "2 requests at once (OLLAMA_NUM_PARALLEL=2), so the second one doesn't wait. Both share the "
            f"same CPU, though, so each got slower (p50 {one.p50:.1f}s → {two.p50:.1f}s) and throughput rose "
            f"only {one.rps:.2f} → {two.rps:.2f} req/s, not 2×."
        )
    base = by_conc.get(2) or levels[0]
    top = levels[-1]
    if top.concurrency > base.concurrency:
        saturated = [level.rps for level in levels if level.concurrency >= base.concurrency]
        shown.append(
            f"From {base.concurrency} to {top.concurrency}, throughput stayed flat "
            f"({min(saturated):.2f}–{max(saturated):.2f} req/s) "
            f"while p50 latency went {base.p50:.1f}s → {top.p50:.1f}s and TTFT {base.ttft50:.1f}s → "
            f"{top.ttft50:.1f}s: the extra time is spent waiting in Ollama's queue, not computing. "
            "TTFT is what a chat user feels, and a queued request hasn't started generating at all."
        )
    lab.summary(
        *shown,
        not_shown="how CPU-based autoscaling misreads this. Once both slots are busy, the CPU looks "
        "the same at 2 or 8 requests, which is why serving stacks scale on queue depth instead "
        "(e.g. vLLM's num_requests_waiting). Also, this is closed-loop load (each worker waits for "
        "its answer), gentler than real open traffic.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrency", default="1,2,4,8", help="comma-separated levels, e.g. 1,2,4,8")
    parser.add_argument("--duration", type=float, default=20, help="seconds per level")
    parser.add_argument("--model", default="chat-small")
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args()
    levels = [int(level) for level in args.concurrency.split(",")]

    lab.require_gateway()
    lab.header(
        "support-bot (long-lived key from `make bootstrap`)",
        f"{PROMPT!r} (every request, streamed, max_tokens={args.max_tokens})",
    )
    print(f"{args.model}, {args.duration:.0f}s per concurrency level\n")
    print(HEADER)
    measured = []
    for concurrency in levels:
        results = asyncio.run(run(concurrency, args.duration, args.model, max_tokens=args.max_tokens))
        measured.append(measure(concurrency, results))
        print(row(measured[-1]), flush=True)
    explain(measured)


if __name__ == "__main__":
    main()
