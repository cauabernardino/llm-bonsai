"""Why gateway replicas need shared state: one rate limit, two pods, with and without Valkey.

1. Two LiteLLM replicas with no Valkey (no shared rate-limit counters, no cache).
2. Two replicas with Valkey.
Each time, a fresh 3-requests/minute key is hit with a burst from a pod *inside* the
cluster, through the litellm Service. (A port-forward would pin every request to one
pod and hide the effect.) Finally the gateway goes back to its normal single replica,
even if something fails on the way.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import lab

ROOT = Path(__file__).resolve().parent.parent
RPM_LIMIT = 3
BURST = 16
CLIENT_IMAGE = "curlimages/curl:8.23.0"
GATEWAY_PODS = "app.kubernetes.io/name=litellm,app.kubernetes.io/component=proxy"
PROMPT = "hi"
ACCESS_LOG = re.compile(r'"POST /v1/chat/completions HTTP/1\.1" (\d{3})')

NO_SHARED_STATE = [
    "--set gateway.replicaCount=2",
    "--set redis.host=",  # no REDIS_HOST: each pod keeps its own rate-limit counters
    "--set gateway.config.proxy_config.litellm_settings.cache=false",  # cache uses Valkey too
]
WITH_VALKEY = ["--set gateway.replicaCount=2"]


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", "llm", *args], check=True, capture_output=True, text=True).stdout


def live_gateway_pods() -> list[str]:
    pods = json.loads(kubectl("get", "pods", "-l", GATEWAY_PODS, "-o", "json"))["items"]
    return [pod["metadata"]["name"] for pod in pods if not pod["metadata"].get("deletionTimestamp")]


def wait_for_old_pods_gone(timeout_s: float = 120) -> None:
    """`helm --wait` returns once the new pods are Ready, while old ones may still be
    shutting down. A port-forward reattaching at that moment can land on a dying pod
    that accepts connections but never answers, so wait until only live pods remain."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pods = json.loads(kubectl("get", "pods", "-l", GATEWAY_PODS, "-o", "json"))["items"]
        if not any(pod["metadata"].get("deletionTimestamp") for pod in pods):
            return
        time.sleep(2)
    raise TimeoutError("old gateway pods still terminating")


def deploy(description: str, overrides: list[str]) -> None:
    print(f"  redeploying the gateway: {description} (helm upgrade, ~1 min)…", flush=True)
    try:
        subprocess.run(
            ["make", "-C", str(ROOT), "litellm", f"LITELLM_ARGS={' '.join(overrides)}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as error:
        sys.exit(f"helm upgrade failed:\n{error.stdout[-1500:]}{error.stderr[-1500:]}")
    wait_for_old_pods_gone()
    deployment = json.loads(kubectl("get", "deploy", "litellm", "-o", "json"))
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    redis = next((e["value"] for e in env if e["name"] == "REDIS_HOST"), None)
    print(f"  gateway: {deployment['status']['readyReplicas']} ready replica(s), REDIS_HOST={redis or 'unset'}")


def burst_from_inside(key: str) -> list[str]:
    """Fire BURST requests from a throwaway in-cluster pod; one new connection each,
    so the Service picks a pod per request."""
    script = (
        f"for i in $(seq 1 {BURST}); do curl -s -o /dev/null -w '%{{http_code}}\\n' "
        "-H 'Connection: close' -H \"Authorization: Bearer $KEY\" -H 'Content-Type: application/json' "
        f"-d '{{\"model\":\"chat-small\",\"max_tokens\":1,\"messages\":[{{\"role\":\"user\",\"content\":\"{PROMPT}\"}}]}}' "
        "http://litellm.llm.svc.cluster.local:4000/v1/chat/completions; done"
    )
    container = {
        "name": "burst",
        "image": CLIENT_IMAGE,
        "command": ["sh", "-c", script],
        "env": [{"name": "KEY", "value": key}],
        "resources": {
            "requests": {"cpu": "10m", "memory": "16Mi"},
            "limits": {"cpu": "200m", "memory": "64Mi"},
        },
    }
    output = kubectl(
        "run", f"replicas-demo-{uuid.uuid4().hex[:6]}", "--rm", "-i", "--quiet",
        "--restart=Never", f"--image={CLIENT_IMAGE}",
        f"--overrides={json.dumps({'spec': {'containers': [container]}})}",
    )
    return [line for line in output.split() if line.isdigit()]


def per_pod_split(since_s: int) -> dict[str, Counter]:
    """Count the burst's responses in each pod's access log (uvicorn logs every request)."""
    return {
        pod: Counter(ACCESS_LOG.findall(kubectl("logs", pod, f"--since={since_s}s")))
        for pod in live_gateway_pods()
    }


def scenario(title: str, description: str, overrides: list[str]) -> tuple[int, str]:
    """Returns (requests allowed in total, allowed per pod as '3 + 3')."""
    print(title)
    deploy(description, overrides)
    with lab.temporary_key("demo-replicas", team="payments-ai", models=["chat-small"], rpm_limit=RPM_LIMIT) as key:
        start = time.monotonic()
        codes = burst_from_inside(key)
        split = per_pod_split(int(time.monotonic() - start) + 5)
    allowed = codes.count("200")
    print(f"  {BURST} requests through the Service → {allowed} × HTTP 200, {codes.count('429')} × HTTP 429")
    for pod, counts in split.items():
        print(f"    {pod}: {counts['200']} × HTTP 200, {counts['429']} × HTTP 429")
    print()
    return allowed, " + ".join(str(counts["200"]) for counts in split.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-replicas", action="store_true", help="leave 2 replicas running")
    args = parser.parse_args()

    lab.require_gateway()
    lab.header(
        f"throwaway key per scenario, team payments-ai: {RPM_LIMIT} requests/min (deleted at the end)",
        f"{PROMPT!r} × {BURST} (max_tokens=1), sent from a pod inside the cluster",
    )
    try:
        without, without_split = scenario("Scenario 1: 2 replicas, no Valkey", "2 replicas, Valkey off", NO_SHARED_STATE)
        with_valkey, with_split = scenario(
            "Scenario 2: 2 replicas, Valkey for shared state", "2 replicas, Valkey on", WITH_VALKEY
        )
    finally:
        if args.keep_replicas:
            print("Leaving 2 replicas running (--keep-replicas).")
        else:
            print("Restoring the normal gateway")
            deploy("1 replica, Valkey on", [])

    lab.summary(
        f"Without Valkey, {without} requests ({without_split} per pod) got through a {RPM_LIMIT}/minute "
        f"limit: each pod counted in its own memory, so the effective limit was up to 2 pods × {RPM_LIMIT}.",
        f"With Valkey, {with_valkey} got through in total ({with_split} per pod): both pods incremented "
        "the same counter, so the limit held no matter which pod served a request.",
        "The burst ran from a pod inside the cluster on purpose: `kubectl port-forward svc/litellm` "
        "sends everything to one pod, so it can't show load balancing.",
        not_shown="the same multiplication for budgets and the response cache, which also live in "
        "Valkey, and what the gateway does when Valkey itself is down.",
    )


if __name__ == "__main__":
    main()
