"""Call a model that is down and watch the gateway fall back to a healthy one.

`chat-broken` points at a host that doesn't exist; router_settings.fallbacks sends
its traffic to `chat-small` instead. The client asked for one model and got an
answer anyway; the response headers and Langfuse show what really happened.
"""

import re
import time
from collections.abc import Mapping

import httpx
from rich.text import Text

from llm_bonsai import report
from llm_bonsai.catalog import CHAT_BROKEN, CHAT_SMALL, PAYMENTS_AI
from llm_bonsai.clients.langfuse import Langfuse, run_id
from llm_bonsai.lab import Lab
from llm_bonsai.models import Observation

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


def show_langfuse(langfuse: Langfuse, user_id: str) -> list[Observation]:
    """Print the run's attempts as Langfuse recorded them; returns them (empty if Langfuse had none)."""
    try:
        with report.console.status("waiting for Langfuse to ingest both attempts…"):
            generations = sorted(langfuse.wait_for_generations(user_id, at_least=2), key=lambda g: g.start_time)
    except (TimeoutError, httpx.HTTPError) as error:
        report.section(f"Langfuse, user {user_id}")
        report.note(f"not available ({error})")
        return []
    first = generations[0].start_time
    attempts = report.table(
        report.right("start"), report.right("took"), "outcome", "backend", title=f"Langfuse, user {user_id}"
    )
    for generation in generations:
        offset_ms = (generation.start_time - first).total_seconds() * 1000
        failed = generation.level == "ERROR"
        attempts.add_row(
            f"+{offset_ms:.0f} ms",
            f"{generation.latency * 1000:.0f} ms",
            Text("ERROR", style="red") if failed else Text("ok", style="green"),
            describe_failure(generation.status_message) if failed else generation.model,
        )
    report.show(attempts)
    report.note(f"UI: `llm-bonsai ui`, then http://localhost:3000 → Tracing → filter User ID = {user_id}")
    return generations


def takeaways(headers: Mapping[str, str], generations: list[Observation]) -> list[str]:
    attempted = headers.get("x-litellm-attempted-fallbacks")
    if not attempted or attempted == "0":
        return [
            f"The headers record no fallback (attempted-fallbacks: {attempted}); check router_settings.fallbacks "
            "in src/llm_bonsai/infra/litellm/values.yaml."
        ]
    bullets = [
        "The client asked for chat-broken and got a normal HTTP 200. Only the response headers say a "
        f"fallback happened (attempted-fallbacks: {attempted}, served by model group "
        f"{headers.get('x-litellm-model-group')})."
    ]
    failed = next((g for g in generations if g.level == "ERROR"), None)
    if failed and len(generations) >= 2:
        bullets += [
            "Langfuse keeps both attempts under one user id. It records the backend, not the model group, "
            "so the ERROR's message (the dead host) is how you tell which deployment failed.",
            f"The dead backend failed in {failed.latency * 1000:.0f} ms, so the fallback cost almost nothing. "
            "A backend that hangs instead costs up to `timeout` (5 s for chat-broken) × (1 + num_retries) "
            "before the fallback starts, which is why chat-broken has both set low.",
        ]
    return bullets


def run(lab: Lab) -> None:
    lab.gateway.require()
    report.header(f"throwaway key, team {PAYMENTS_AI.alias} (deleted at the end)", repr(PROMPT))
    user_id = run_id("fallback")
    models = [CHAT_BROKEN, CHAT_SMALL]
    with lab.gateway.temporary_key("probe-fallback", team=PAYMENTS_AI.alias, models=models) as key:
        start = time.perf_counter()
        raw = lab.gateway.client(key).chat.completions.with_raw_response.create(
            model=CHAT_BROKEN, messages=[{"role": "user", "content": PROMPT}], user=user_id
        )
        elapsed = time.perf_counter() - start

    report.line("Requested model chat-broken → ", report.http(raw.http_response.status_code), f" in {elapsed:.2f}s")
    headers = report.table("response header", "value")
    for name in FALLBACK_HEADERS:
        headers.add_row(name, str(raw.headers.get(name)))
    report.console.print(headers)
    report.line(("Answer: ", "bold"), (raw.parse().choices[0].message.content or "").strip())

    generations = show_langfuse(lab.langfuse, user_id)
    report.summary(*takeaways(raw.headers, generations))
