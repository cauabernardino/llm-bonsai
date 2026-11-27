"""Delete the gateway pod in the middle of traffic and measure the damage.

Runs the load test at low concurrency, deletes the LiteLLM pod after a few seconds,
watches for the replacement pod to become Ready, and reports how long requests failed
and where that time went. Needs `make pf` running: it reattaches localhost:4000 to the
replacement pod.
"""

import asyncio
import json
import subprocess
import time

import lab
import load_test

CONCURRENCY = 2
DURATION_S = 90
KILL_AT_S = 15
BUCKET_S = 5
GATEWAY_PODS = "app.kubernetes.io/name=litellm,app.kubernetes.io/component=proxy"


def gateway_pods() -> list[dict]:
    out = subprocess.run(
        ["kubectl", "-n", "llm", "get", "pod", "-l", GATEWAY_PODS, "-o", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [pod for pod in json.loads(out)["items"] if not pod["metadata"].get("deletionTimestamp")]


def is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


async def wait_for_replacement(killed: str, t0: float) -> float:
    """Seconds since t0 at which a gateway pod other than `killed` is Ready."""
    while True:
        pods = await asyncio.to_thread(gateway_pods)
        if any(pod["metadata"]["name"] != killed and is_ready(pod) for pod in pods):
            return time.perf_counter() - t0
        await asyncio.sleep(0.5)


async def chaos() -> tuple[list[load_test.Result], float, float]:
    t0 = time.perf_counter()
    load = asyncio.create_task(load_test.run(CONCURRENCY, DURATION_S, max_tokens=32))
    await asyncio.sleep(KILL_AT_S)
    pod = (await asyncio.to_thread(gateway_pods))[0]["metadata"]["name"]
    killed_at = time.perf_counter() - t0
    await asyncio.to_thread(
        subprocess.run, ["kubectl", "-n", "llm", "delete", "pod", pod, "--wait=false"], check=True, capture_output=True
    )
    print(f"t={killed_at:5.1f}s  deleted pod {pod}", flush=True)
    ready_at = await wait_for_replacement(pod, t0)
    print(f"t={ready_at:5.1f}s  replacement pod Ready", flush=True)
    return await load, killed_at, ready_at


def timeline(results: list[load_test.Result]) -> None:
    print(f"\nRequests by start time ({BUCKET_S}s buckets):")
    last = max(r.started for r in results)
    for start in range(0, int(last) + 1, BUCKET_S):
        bucket = [r for r in results if start <= r.started < start + BUCKET_S]
        if not bucket:
            print(f"  {start:>3}–{start + BUCKET_S:<3}s  none started (workers stuck on connections to the dead pod)")
            continue
        ok = sum(r.error is None for r in bucket)
        failed = f", {len(bucket) - ok} failed" if len(bucket) > ok else ""
        print(f"  {start:>3}–{start + BUCKET_S:<3}s  {ok} ok{failed}")


def main() -> None:
    lab.require_gateway()
    lab.header(
        "support-bot (long-lived key from `make bootstrap`)",
        f"{load_test.PROMPT!r} ({CONCURRENCY} concurrent streams for {DURATION_S}s, max_tokens=32)",
    )
    print(f"The gateway pod gets deleted at t={KILL_AT_S}s.\n")
    results, killed_at, ready_at = asyncio.run(chaos())
    timeline(results)

    failed = [r for r in results if r.error]
    if not failed:
        print("\nNo request failed.")
        return
    cut_off = [r for r in failed if r.started < killed_at]
    last_failure = max(r.started for r in failed)
    recovered = min((r.started for r in results if r.error is None and r.started > last_failure), default=None)
    print(
        f"\nFailed attempts: {len(failed)} ({', '.join(sorted({r.error for r in failed}))}). Workers retry "
        f"every {load_test.ERROR_BACKOFF_S}s, so this counts retries, not users."
    )
    if recovered is None:
        print("Requests never succeeded again within the run.")
        return
    outage, startup, reattach = recovered - killed_at, ready_at - killed_at, recovered - ready_at
    # Readiness is polled (~1s resolution) and requests are timed by when they started, so a
    # gap under a second means "the port-forward picked the new pod up right away".
    if reattach < 1:
        breakdown = "nearly all of it waiting for the replacement pod; the port-forward picked it up right away"
    else:
        breakdown = f"{startup:.0f}s waiting for the replacement pod + {reattach:.0f}s for the port-forward to reattach"
    print(f"Outage: {outage:.1f}s, {breakdown} (timings ±1s)")

    lab.summary(
        f"With one replica, deleting the pod is a full outage: {outage:.0f}s with no successful request, "
        f"{breakdown}. That's Kubernetes creating a new gateway pod, LiteLLM starting up, and its "
        "readiness probe passing.",
        (
            f"{len(cut_off)} request(s) were mid-stream on the deleted pod and were cut off. Retrying "
            "those is the client's job, and only safe if the request is idempotent."
            if cut_off
            else "Requests already in flight when the pod was deleted finished normally: on SIGTERM the "
            "server stops taking new connections but completes what it has (graceful shutdown). "
            "It was the new requests that failed or hung."
        ),
        not_shown="the same failure with 2+ replicas. New connections would go to the surviving pod, "
        "so only the deleted pod's in-flight requests fail (plus any sent in the second or two "
        "before its endpoint is removed). Pair that with a PodDisruptionBudget and a draining "
        "preStop hook (LiteLLM's /health/drain) so upgrades finish their streams first.",
    )


if __name__ == "__main__":
    main()
