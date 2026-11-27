"""Delete the gateway pod in the middle of traffic and measure the damage.

Runs the load generator at low concurrency, deletes a LiteLLM pod after a few seconds,
watches for a replacement to become Ready, and reports how long requests failed and
where that time went. The CLI's port-forward reattaches to the replacement pod by itself.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from llm_bonsai import gateway, kube, report
from llm_bonsai.probe import load
from llm_bonsai.probe.load import Result

CONCURRENCY = 2
DURATION_S = 90
KILL_AT_S = 15
BUCKET_S = 5
IDLE_BUCKET = "each worker was still waiting on an earlier request"


@dataclass(frozen=True)
class Damage:
    failed: int
    error_kinds: list[str]
    cut_off: int  # failed requests that had started before the pod was deleted
    recovered_at: float | None  # first success after the last failure, seconds since start


def assess(results: list[Result], killed_at: float) -> Damage:
    failed = [r for r in results if r.error]
    last_failure = max((r.started for r in failed), default=killed_at)
    successes_after = [r.started for r in results if r.error is None and r.started > last_failure]
    return Damage(
        failed=len(failed),
        error_kinds=sorted({r.error for r in failed if r.error}),
        cut_off=sum(r.started < killed_at for r in failed),
        recovered_at=min(successes_after, default=None),
    )


def is_ready(pod: dict[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


async def wait_for_replacement(killed: str, t0: float) -> float:
    """Seconds since t0 at which a gateway pod other than `killed` is Ready."""
    while True:
        pods = await asyncio.to_thread(kube.gateway_pods)
        if any(pod["metadata"]["name"] != killed and is_ready(pod) for pod in pods):
            return time.perf_counter() - t0
        await asyncio.sleep(0.5)


async def kill_during_load() -> tuple[list[Result], int, float, float]:
    """Returns (every request, gateway pods before the kill, when the pod was deleted, when a replacement was Ready)."""
    t0 = time.perf_counter()
    traffic = asyncio.create_task(load.generate_load(CONCURRENCY, DURATION_S, max_tokens=32))
    await asyncio.sleep(KILL_AT_S)
    pods = await asyncio.to_thread(kube.gateway_pods)
    pod = pods[0]["metadata"]["name"]
    killed_at = time.perf_counter() - t0
    await asyncio.to_thread(kube.kubectl, "delete", "pod", pod, "--wait=false", namespace="llm")
    print(f"t={killed_at:5.1f}s  deleted pod {pod}", flush=True)
    ready_at = await wait_for_replacement(pod, t0)
    print(f"t={ready_at:5.1f}s  replacement pod Ready", flush=True)
    return await traffic, len(pods), killed_at, ready_at


def timeline(results: list[Result]) -> None:
    print(f"\nRequests by start time ({BUCKET_S}s buckets):")
    last = max(r.started for r in results)
    for start in range(0, int(last) + 1, BUCKET_S):
        bucket = [r for r in results if start <= r.started < start + BUCKET_S]
        if not bucket:
            print(f"  {start:>3}–{start + BUCKET_S:<3}s  none started ({IDLE_BUCKET})")
            continue
        ok = sum(r.error is None for r in bucket)
        failed = f", {len(bucket) - ok} failed" if len(bucket) > ok else ""
        print(f"  {start:>3}–{start + BUCKET_S:<3}s  {ok} ok{failed}")


NOT_SHOWN = (
    "the same failure with 2+ replicas. New connections would go to the surviving pod, so only the deleted "
    "pod's in-flight requests fail (plus any sent in the second or two before its endpoint is removed). Pair "
    "that with a PodDisruptionBudget and a draining preStop hook (LiteLLM's /health/drain) so upgrades finish "
    "their streams first."
)


def run() -> None:
    """Delete the gateway pod under load and measure the outage (~2 min)."""
    gateway.require_gateway()
    report.header(
        "support-bot (long-lived key from `llm-bonsai setup teams`)",
        f"{load.PROMPT!r} ({CONCURRENCY} concurrent streams for {DURATION_S}s, max_tokens=32)",
    )
    print(f"A gateway pod gets deleted at t={KILL_AT_S}s.\n")
    results, pods_before, killed_at, ready_at = asyncio.run(kill_during_load())
    timeline(results)
    damage = assess(results, killed_at)

    if not damage.failed:
        report.summary(
            f"No request failed: {pods_before} gateway pods were Ready, so traffic moved to the survivors. "
            "Run with a single replica to see the outage."
        )
        return
    print(
        f"\nFailed attempts: {damage.failed} ({', '.join(damage.error_kinds)}). "
        f"Workers retry every {load.ERROR_BACKOFF_S}s, so this counts retries, not users."
    )
    if damage.recovered_at is None:
        report.summary(
            f"Requests never succeeded again within the {DURATION_S}s run, although a replacement pod was "
            f"Ready at t={ready_at:.0f}s. Check `kubectl -n llm get pods` and the gateway's logs."
        )
        return

    outage, startup = damage.recovered_at - killed_at, ready_at - killed_at
    reattach = damage.recovered_at - ready_at
    # Readiness is polled (~1s resolution) and requests are timed by when they started, so a
    # gap under a second means "the port-forward picked the new pod up right away".
    if reattach < 1:
        breakdown = "nearly all of it waiting for the replacement pod; the port-forward picked it up right away"
    else:
        breakdown = f"{startup:.0f}s waiting for the replacement pod + {reattach:.0f}s for the port-forward to reattach"
    print(f"Outage: {outage:.1f}s, {breakdown} (timings ±1s)")

    replicas = "one replica" if pods_before == 1 else f"{pods_before} replicas"
    cut_off = "1 request was" if damage.cut_off == 1 else f"{damage.cut_off} requests were"
    report.summary(
        f"With {replicas}, deleting a pod was a full outage: {outage:.0f}s with no successful request, "
        f"{breakdown}. That's Kubernetes creating a new gateway pod, LiteLLM starting up, and its readiness "
        "probe passing.",
        (
            f"{cut_off} mid-stream on the deleted pod and got cut off. Retrying those is the client's job, "
            "and only safe if the request is idempotent."
            if damage.cut_off
            else "Requests already in flight when the pod was deleted finished normally: on SIGTERM the "
            "server stops taking new connections but completes what it has (graceful shutdown). "
            "It was the new requests that failed or hung."
        ),
        not_shown=NOT_SHOWN,
    )
