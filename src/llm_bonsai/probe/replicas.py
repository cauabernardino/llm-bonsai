"""Why gateway replicas need shared state: one rate limit, two pods, with and without Valkey.

1. Two LiteLLM replicas with no Valkey (no shared rate-limit counters, no cache).
2. Two replicas with Valkey.
Each time, a fresh 3-requests/minute key is hit with a burst from a pod *inside* the
cluster, through the litellm Service. (A port-forward would pin every request to one
pod and hide the effect.) Finally the gateway goes back to its normal single replica,
even if something fails on the way.
"""

import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass

from llm_bonsai import report
from llm_bonsai.catalog import CHAT_SMALL, GATEWAY, PAYMENTS_AI
from llm_bonsai.errors import LabError
from llm_bonsai.lab import Lab

RPM_LIMIT = 3
BURST = 16
CLIENT_IMAGE = "curlimages/curl:8.23.0"
PROMPT = "hi"
SERVICE_URL = f"http://{GATEWAY.name}.{GATEWAY.namespace}.svc.cluster.local:{GATEWAY.port}"
ACCESS_LOG = re.compile(r'"POST /v1/chat/completions HTTP/1\.1" (\d{3})')

NO_SHARED_STATE = [
    "gateway.replicaCount=2",
    "redis.host=",  # no REDIS_HOST: each pod keeps its own rate-limit counters
    "gateway.config.proxy_config.litellm_settings.cache=false",  # the cache uses Valkey too
]
WITH_VALKEY = ["gateway.replicaCount=2"]


@dataclass(frozen=True)
class Outcome:
    allowed: int
    per_pod: str  # allowed per pod, e.g. "3 + 3"


def wait_for_old_pods_gone(lab: Lab, timeout_s: float = 120) -> None:
    """`helm --wait` returns once the new pods are Ready, while old ones may still be
    shutting down. A port-forward reattaching at that moment can land on a dying pod
    that accepts connections but never answers, so wait until only live pods remain."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not any(pod.terminating for pod in lab.gateway.pods(include_terminating=True)):
            return
        time.sleep(2)
    raise LabError("old gateway pods still terminating")


def deploy(lab: Lab, description: str, set_values: list[str]) -> None:
    print(f"  redeploying the gateway: {description} (helm upgrade, ~1 min)…", flush=True)
    lab.installer.install_litellm(set_values, quiet=True)
    wait_for_old_pods_gone(lab)
    # The CLI's own port-forward was pinned to a pod that is now gone; reconnect to a live one.
    lab.gateway.reconnect()
    lab.gateway.require(wait_s=90)
    deployment = lab.kube.kubectl_json("get", "deploy", GATEWAY.name, namespace=GATEWAY.namespace)
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    redis = next((e["value"] for e in env if e["name"] == "REDIS_HOST"), None)
    print(f"  gateway: {deployment['status']['readyReplicas']} ready replica(s), REDIS_HOST={redis or 'unset'}")


def burst_from_inside(lab: Lab, key: str) -> list[str]:
    """Fire BURST requests from a throwaway in-cluster pod; one new connection each,
    so the Service picks a pod per request. Returns the HTTP status codes.

    The key goes in on stdin (`kubectl run -i`), never into the pod spec or argv.
    """
    body = json.dumps({"model": CHAT_SMALL, "max_tokens": 1, "messages": [{"role": "user", "content": PROMPT}]})
    script = (
        f"read -r KEY; for i in $(seq 1 {BURST}); do curl -s -o /dev/null -w '%{{http_code}}\\n' "
        "-H 'Connection: close' -H \"Authorization: Bearer $KEY\" -H 'Content-Type: application/json' "
        f"-d '{body}' {SERVICE_URL}/v1/chat/completions; done"
    )
    container = {
        "name": "burst",
        "image": CLIENT_IMAGE,
        "command": ["sh", "-c", script],
        # --overrides replaces the container `run -i` generates, so ask for stdin here too.
        "stdin": True,
        "stdinOnce": True,
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
    }
    output = lab.kube.kubectl(
        "run", f"replicas-probe-{uuid.uuid4().hex[:6]}", "--rm", "-i", "--quiet", "--restart=Never",
        f"--image={CLIENT_IMAGE}", f"--overrides={json.dumps({'spec': {'containers': [container]}})}",
        namespace=GATEWAY.namespace, stdin=f"{key}\n",
    )  # fmt: skip
    return [line for line in output.split() if line.isdigit()]


def per_pod_split(lab: Lab, since_s: int) -> dict[str, Counter[str]]:
    """Count the burst's responses in each pod's access log (uvicorn logs every request)."""
    return {
        pod.name: Counter(
            ACCESS_LOG.findall(lab.kube.kubectl("logs", pod.name, f"--since={since_s}s", namespace=GATEWAY.namespace))
        )
        for pod in lab.gateway.pods()
    }


def scenario(lab: Lab, title: str, description: str, set_values: list[str]) -> Outcome:
    print(title)
    deploy(lab, description, set_values)
    limits = {"models": [CHAT_SMALL], "rpm_limit": RPM_LIMIT}
    with lab.gateway.temporary_key("probe-replicas", team=PAYMENTS_AI.alias, **limits) as key:
        start = time.monotonic()
        codes = burst_from_inside(lab, key)
        split = per_pod_split(lab, int(time.monotonic() - start) + 5)
    allowed = codes.count("200")
    print(f"  {BURST} requests through the Service → {allowed} × HTTP 200, {codes.count('429')} × HTTP 429")
    for pod, counts in split.items():
        print(f"    {pod}: {counts['200']} × HTTP 200, {counts['429']} × HTTP 429")
    print()
    return Outcome(allowed, " + ".join(str(counts["200"]) for counts in split.values()))


def takeaways(without: Outcome, with_valkey: Outcome) -> list[str]:
    if without.allowed > RPM_LIMIT:
        separate = (
            f"Without Valkey, {without.allowed} requests ({without.per_pod} per pod) got through a "
            f"{RPM_LIMIT}/minute limit: each pod counted in its own memory, so the effective limit grew with "
            "the number of pods."
        )
    else:
        separate = (
            f"Without Valkey, only {without.allowed} requests ({without.per_pod} per pod) got through: the "
            "Service sent too few requests to one of the pods for its own counter to matter. Run it again."
        )
    if with_valkey.allowed <= RPM_LIMIT:
        shared = (
            f"With Valkey, {with_valkey.allowed} got through in total ({with_valkey.per_pod} per pod): both pods "
            "incremented the same counter, so the limit held no matter which pod served a request."
        )
    else:
        shared = (
            f"With Valkey, {with_valkey.allowed} got through ({with_valkey.per_pod} per pod), more than the "
            f"limit of {RPM_LIMIT}: the pods didn't share a counter. Check REDIS_HOST on the gateway."
        )
    return [
        separate,
        shared,
        "The burst ran from a pod inside the cluster on purpose: `kubectl port-forward svc/litellm` "
        "sends everything to one pod, so it can't show load balancing.",
    ]


def run(lab: Lab, keep_replicas: bool) -> None:
    lab.gateway.require()
    report.header(
        f"throwaway key per scenario, team {PAYMENTS_AI.alias}: {RPM_LIMIT} requests/min (deleted at the end)",
        f"{PROMPT!r} × {BURST} (max_tokens=1), sent from a pod inside the cluster",
    )
    try:
        without = scenario(lab, "Scenario 1: 2 replicas, no Valkey", "2 replicas, Valkey off", NO_SHARED_STATE)
        with_valkey = scenario(
            lab, "Scenario 2: 2 replicas, Valkey for shared state", "2 replicas, Valkey on", WITH_VALKEY
        )
    finally:
        if keep_replicas:
            print("Leaving 2 replicas running (--keep-replicas).")
        else:
            print("Restoring the normal gateway")
            deploy(lab, "1 replica, Valkey on", [])

    report.summary(
        *takeaways(without, with_valkey),
        not_shown="the same multiplication for budgets and the response cache, which also live in "
        "Valkey, and what the gateway does when Valkey itself is down.",
    )
