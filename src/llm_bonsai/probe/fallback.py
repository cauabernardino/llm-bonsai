"""Call a model that is down and watch the gateway fall back to a healthy one.

`chat-broken` points at a host that doesn't exist; router_settings.fallbacks sends
its traffic to `chat-small` instead. The client asked for one model and got an
answer anyway; the response headers and Langfuse show what really happened.
"""

import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from llm_bonsai import gateway, report, traces

PROMPT = "In one sentence: what is a payment refund?"
FALLBACK_HEADERS = ["x-litellm-model-group", "x-litellm-attempted-fallbacks", "x-litellm-model-api-base"]


def describe_failure(status_message: str) -> str:
    """'...Cannot connect to host does-not-exist...:1234 ssl:<...> [Name or service not known]'
    → 'does-not-exist...:1234 [Name or service not known]'"""
    host = re.search(r"host (\S+)", status_message)
    reason = re.search(r"\[([^\]]+)\]\s*$", status_message)
    if not host:
        return status_message[:80]
    return f"{host.group(1)} [{reason.group(1)}]" if reason else host.group(1)


def show_langfuse(user_id: str) -> list[dict[str, Any]]:
    """Print the run's attempts as Langfuse recorded them; returns them (empty if Langfuse had none)."""
    print(f"\nLangfuse, user {user_id}:")
    try:
        generations = sorted(traces.wait_for_generations(user_id, at_least=2), key=lambda g: g["startTime"])
    except (TimeoutError, httpx.HTTPError) as error:
        print(f"  not available ({error})")
        return []
    first = datetime.fromisoformat(generations[0]["startTime"])
    print(f"  {'start':>7} {'took':>8}  outcome")
    for generation in generations:
        offset_ms = (datetime.fromisoformat(generation["startTime"]) - first).total_seconds() * 1000
        if generation["level"] == "ERROR":
            outcome = f"ERROR  {describe_failure(generation['statusMessage'])}"
        else:
            outcome = f"ok     {generation['model']}"
        print(f"  {f'+{offset_ms:.0f}ms':>7} {generation['latency'] * 1000:6.0f}ms  {outcome}")
    print(f"  UI: `llm-bonsai ui`, then http://localhost:3000 → Tracing → filter User ID = {user_id}")
    return generations


def takeaways(headers: Mapping[str, str], generations: list[dict[str, Any]]) -> list[str]:
    attempted = headers.get("x-litellm-attempted-fallbacks")
    if not attempted or attempted == "0":
        return [
            f"The headers record no fallback (attempted-fallbacks: {attempted}); check router_settings.fallbacks "
            "in infra/litellm/values.yaml."
        ]
    bullets = [
        "The client asked for chat-broken and got a normal HTTP 200. Only the response headers say a "
        f"fallback happened (attempted-fallbacks: {attempted}, served by model group "
        f"{headers.get('x-litellm-model-group')})."
    ]
    failed = next((g for g in generations if g["level"] == "ERROR"), None)
    if failed and len(generations) >= 2:
        bullets += [
            "Langfuse keeps both attempts under one user id. It records the backend, not the model group, "
            "so the ERROR's message (the dead host) is how you tell which deployment failed.",
            f"The dead backend failed in {failed['latency'] * 1000:.0f} ms, so the fallback cost almost nothing. "
            "A backend that hangs instead costs up to `timeout` (5 s for chat-broken) × (1 + num_retries) "
            "before the fallback starts, which is why chat-broken has both set low.",
        ]
    return bullets


def run() -> None:
    """Call chat-broken and show the gateway's fallback to chat-small."""
    gateway.require_gateway()
    report.header("throwaway key, team payments-ai (deleted at the end)", repr(PROMPT))
    user_id = traces.run_id("fallback")
    with gateway.temporary_key("probe-fallback", team="payments-ai", models=["chat-broken", "chat-small"]) as key:
        start = time.perf_counter()
        raw = gateway.client(key).chat.completions.with_raw_response.create(
            model="chat-broken", messages=[{"role": "user", "content": PROMPT}], user=user_id
        )
        elapsed = time.perf_counter() - start

    print(f"Requested model chat-broken → HTTP {raw.http_response.status_code} in {elapsed:.2f}s")
    print("Response headers:")
    for name in FALLBACK_HEADERS:
        print(f"  {name}: {raw.headers.get(name)}")
    print(f"Answer: {(raw.parse().choices[0].message.content or '').strip()}")

    generations = show_langfuse(user_id)
    report.summary(*takeaways(raw.headers, generations))
