"""Call a model that is down and watch the gateway fall back to a healthy one.

`chat-broken` points at a host that doesn't exist; router_settings.fallbacks sends
its traffic to `chat-small` instead. The client asked for one model and got an
answer anyway; the response headers and Langfuse show what really happened.
"""

import re
import time
from datetime import datetime

import httpx

import lab

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


def show_langfuse(user_id: str) -> float | None:
    """Print both attempts; return the failed attempt's latency in seconds."""
    print(f"\nLangfuse, user {user_id}:")
    try:
        generations = sorted(lab.wait_for_generations(user_id, at_least=2), key=lambda g: g["startTime"])
    except (TimeoutError, httpx.HTTPError) as error:
        print(f"  not available ({error}); is the :3000 port-forward up?")
        return None
    first = datetime.fromisoformat(generations[0]["startTime"])
    failed_latency = None
    print("    start     took  outcome")
    for gen in generations:
        offset_ms = (datetime.fromisoformat(gen["startTime"]) - first).total_seconds() * 1000
        latency_ms = gen["latency"] * 1000
        if gen["level"] == "ERROR":
            failed_latency = gen["latency"]
            outcome = f"ERROR  {describe_failure(gen['statusMessage'])}"
        else:
            outcome = f"ok     {gen['model']}"
        print(f"  +{offset_ms:4.0f}ms {latency_ms:6.0f}ms  {outcome}")
    print(f"  UI: {lab.LANGFUSE_URL} → Tracing → filter User ID = {user_id}")
    return failed_latency


def main() -> None:
    lab.header("throwaway key, team payments-ai (deleted at the end)", repr(PROMPT))
    user_id = lab.run_id("fallback")
    with lab.temporary_key("demo-fallback", team="payments-ai", models=["chat-broken", "chat-small"]) as key:
        start = time.perf_counter()
        raw = lab.gateway(key).chat.completions.with_raw_response.create(
            model="chat-broken",
            messages=[{"role": "user", "content": PROMPT}],
            user=user_id,
        )
        elapsed = time.perf_counter() - start

    print(f"Requested model chat-broken → HTTP {raw.http_response.status_code} in {elapsed:.2f}s")
    print("Response headers:")
    for name in FALLBACK_HEADERS:
        print(f"  {name}: {raw.headers.get(name)}")
    print(f"Answer: {raw.parse().choices[0].message.content.strip()}")

    failed_latency = show_langfuse(user_id)
    speed = (
        f"The dead backend failed in {failed_latency * 1000:.0f} ms (DNS error), so the fallback cost "
        "almost nothing."
        if failed_latency is not None
        else "The dead backend failed on a DNS error, so the fallback cost almost nothing."
    )
    lab.summary(
        "The client asked for chat-broken and got a normal HTTP 200. Only the response headers "
        f"say a fallback happened (attempted-fallbacks: {raw.headers.get('x-litellm-attempted-fallbacks')}, "
        f"served by model group {raw.headers.get('x-litellm-model-group')}).",
        "Langfuse keeps both attempts under one user id. It records the backend, not the model "
        "group, so the ERROR's message (the dead host) is how you tell which deployment failed.",
        f"{speed} A backend that hangs instead costs up to `timeout` (5 s for chat-broken) × "
        "(1 + num_retries) before the fallback starts, which is why chat-broken has both set low.",
    )


if __name__ == "__main__":
    main()
