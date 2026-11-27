"""Closed-loop load test against the gateway: N workers, each streaming one request
after another for a fixed duration.

Reports latency, TTFT (time to the first chunk with non-empty delta.content) and the
error rate per concurrency level. Run a sweep to watch requests queue once concurrency
passes what the model server runs in parallel (OLLAMA_NUM_PARALLEL=2 here).
"""

import asyncio
import json
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from llm_bonsai import report
from llm_bonsai.catalog import CHAT_SMALL, SUPPORT_BOT
from llm_bonsai.lab import Lab
from llm_bonsai.models import Attempt

PROMPT = "Explain in three sentences why the sky is blue."
# A failing request returns instantly; pause so a dead gateway doesn't become a hot loop.
ERROR_BACKOFF_S = 0.25


class LoadGenerator:
    """Closed-loop streaming traffic to one model with one key."""

    def __init__(self, base_url: str, key: str, model: str = CHAT_SMALL, max_tokens: int = 64) -> None:
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self._key = key

    async def run(self, concurrency: int, duration: float) -> list[Attempt]:
        """Run `concurrency` workers for `duration` seconds and return every attempt."""
        attempts: list[Attempt] = []
        t0 = time.perf_counter()

        async def worker(client: httpx.AsyncClient) -> None:
            while (begin := time.perf_counter()) - t0 < duration:
                try:
                    ttft, error = await self._one_request(client)
                except httpx.HTTPError as exc:
                    ttft, error = None, type(exc).__name__
                attempts.append(Attempt(begin - t0, time.perf_counter() - begin, ttft, error))
                if error:
                    await asyncio.sleep(ERROR_BACKOFF_S)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=httpx.Timeout(120, connect=5),
        ) as client:
            await asyncio.gather(*(worker(client) for _ in range(concurrency)))
        return attempts

    async def _one_request(self, client: httpx.AsyncClient) -> tuple[float | None, str | None]:
        """Stream one completion; returns (ttft, error)."""
        body = {
            "model": self.model,
            "stream": True,
            "max_tokens": self.max_tokens,
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
                choices = json.loads(line.removeprefix("data: ")).get("choices") or [{}]
                if ttft is None and choices[0].get("delta", {}).get("content"):
                    ttft = time.perf_counter() - start
        return ttft, None


def support_bot_load(lab: Lab, model: str = CHAT_SMALL, max_tokens: int = 64) -> LoadGenerator:
    return LoadGenerator(lab.gateway.base_url, lab.env.get(SUPPORT_BOT.env_var), model, max_tokens)


def percentile(values: Sequence[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


@dataclass(frozen=True)
class Level:
    """One concurrency level's measurements."""

    concurrency: int
    attempts: int
    ok: int
    rps: float
    p50: float | None
    p95: float | None
    ttft50: float | None
    ttft95: float | None
    errors: Counter[str]


def measure(concurrency: int, results: list[Attempt]) -> Level:
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


def seconds(value: float | None) -> str:
    return f"{value:7.2f}s" if value is not None else f"{'–':>8}"


HEADER = f"{'conc':>4} {'reqs':>5} {'req/s':>6} {'p50 lat':>8} {'p95 lat':>8} {'p50 ttft':>8} {'p95 ttft':>8}  errors"


def row(level: Level) -> str:
    errors = "0"
    if level.errors:
        kinds = ", ".join(f"{kind} ×{count}" for kind, count in level.errors.most_common())
        errors = f"{level.errors.total()} ({kinds})"
    return (
        f"{level.concurrency:>4} {level.attempts:>5} {level.rps:>6.2f} {seconds(level.p50)} {seconds(level.p95)} "
        f"{seconds(level.ttft50)} {seconds(level.ttft95)}  {errors}"
    )


LEGEND = "lat = time to the full answer, ttft = time to first token; p50 = median, p95 = 95th percentile"
SERVER_NAME = {CHAT_SMALL: "Ollama"}  # anything else is described generically


def takeaways(levels: list[Level], model: str) -> list[str]:
    """What the sweep showed. Assumes every level has successful requests, so its percentiles are set."""
    server = SERVER_NAME.get(model, "the model server")
    by_concurrency = {level.concurrency: level for level in levels}
    bullets = []
    if model == CHAT_SMALL and 1 in by_concurrency and 2 in by_concurrency:
        one, two = by_concurrency[1], by_concurrency[2]
        assert one.ttft50 is not None and two.ttft50 is not None and one.p50 is not None and two.p50 is not None
        if two.ttft50 <= max(2 * one.ttft50, 0.5):
            ttft = (
                f"At 2 concurrent requests TTFT stayed low ({one.ttft50:.2f}s → {two.ttft50:.2f}s): Ollama works "
                "on 2 requests at once (OLLAMA_NUM_PARALLEL=2), so the second one doesn't wait."
            )
        else:
            ttft = f"At 2 concurrent requests TTFT already rose ({one.ttft50:.2f}s → {two.ttft50:.2f}s)."
        scaling = (
            f"throughput rose only {one.rps:.2f} → {two.rps:.2f} req/s, not 2×"
            if two.rps < 1.8 * one.rps
            else f"throughput went {one.rps:.2f} → {two.rps:.2f} req/s"
        )
        bullets.append(f"{ttft} Each request got slower (p50 {one.p50:.1f}s → {two.p50:.1f}s) and {scaling}.")
    base = by_concurrency.get(2, levels[0])
    top = levels[-1]
    if top.concurrency > base.concurrency:
        assert base.p50 is not None and top.p50 is not None and base.ttft50 is not None and top.ttft50 is not None
        rates = [level.rps for level in levels if level.concurrency >= base.concurrency]
        latency = f"p50 latency went {base.p50:.1f}s → {top.p50:.1f}s and TTFT {base.ttft50:.1f}s → {top.ttft50:.1f}s"
        if max(rates) <= 1.2 * base.rps and top.p50 > base.p50:
            bullets.append(
                f"From {base.concurrency} to {top.concurrency} concurrent requests, throughput stopped growing "
                f"({min(rates):.2f}–{max(rates):.2f} req/s) while {latency}: the extra time is spent waiting in "
                f"{server}'s queue, not computing. TTFT is what a chat user feels, and a queued request hasn't "
                "started generating at all."
            )
        else:
            bullets.append(
                f"From {base.concurrency} to {top.concurrency} concurrent requests, throughput went "
                f"{base.rps:.2f} → {top.rps:.2f} req/s while {latency}."
            )
    return bullets


def run(lab: Lab, levels: list[int], duration: float, model: str, max_tokens: int) -> None:
    lab.gateway.require()
    generator = support_bot_load(lab, model, max_tokens)
    report.header(
        f"{SUPPORT_BOT.alias} (long-lived key from `llm-bonsai setup teams`)",
        f"{PROMPT!r} (every request, streamed, max_tokens={max_tokens})",
    )
    print(f"{model}, {duration:.0f}s per concurrency level")
    print(f"{LEGEND}\n")
    print(HEADER)
    measured = []
    for concurrency in levels:
        results = asyncio.run(generator.run(concurrency, duration))
        measured.append(measure(concurrency, results))
        print(row(measured[-1]), flush=True)

    if any(level.ok == 0 for level in measured):
        print(
            "\nSome levels had no successful requests; see the errors column "
            "(HTTP 401/403: the key can't use this model; ConnectError: the gateway went away)."
        )
        return
    if any(level.ok < 20 for level in measured):
        print("\n(p95 over fewer than 20 requests is close to the slowest one; use --duration for more.)")
    report.summary(
        *takeaways(measured, model),
        not_shown="how CPU-based autoscaling misreads this. Once the server is saturated, its CPU looks "
        "the same at 2 or 8 requests, which is why serving stacks scale on queue depth instead "
        "(e.g. vLLM's num_requests_waiting). Also, this is closed-loop load (each worker waits for "
        "its answer), gentler than real open traffic.",
    )
