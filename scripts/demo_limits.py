"""Hit a key's rate limit, then spend through its budget.

Runs on a throwaway key with the payments-ai limits (3 requests/minute, $0.05 budget),
so it can be re-run any time without exhausting the long-lived payments-ai key.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import openai

import lab

RPM_LIMIT = 3
BUDGET = 0.05
QUICK_REQUESTS = 10
QUICK_PROMPT = "hi"
# ~425 prompt tokens: at the fake $0.0001/token, each request costs ~$0.04.
LONG_PROMPT = "Summarise this refund policy in one sentence. " + (
    "Customers may return items within thirty days for a full refund. " * 30
)


@dataclass
class Crossing:
    request: int
    before: float
    after: float


def seconds_until_reset(error: openai.APIStatusError) -> float:
    """LiteLLM's 429 says when the window resets: 'Limit resets at: 2026-11-27 20:45:56 UTC'."""
    found = re.search(r"Limit resets at: ([\d-]+ [\d:]+) UTC", str(error))
    if not found:
        return 60
    reset_at = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return max(0.0, (reset_at - datetime.now(UTC)).total_seconds()) + 1


def wait_for_window(seconds: float) -> None:
    print(f"\n… waiting {seconds:.0f}s for the rate-limit window to reset\n")
    time.sleep(seconds)


def rate_limit_part(client: openai.OpenAI) -> tuple[int, str | None, float, float]:
    """Returns (allowed, retry-after header, seconds until the window resets, spend)."""
    print(f"Part 1: rate limit, {QUICK_REQUESTS} requests in a row")
    allowed, retry_after, reset_in, spent = 0, None, 60.0, 0.0
    for n in range(1, QUICK_REQUESTS + 1):
        try:
            raw = client.chat.completions.with_raw_response.create(
                model="chat-small", max_tokens=1, messages=[{"role": "user", "content": QUICK_PROMPT}]
            )
        except openai.RateLimitError as error:
            if retry_after is None:
                retry_after = error.response.headers.get("retry-after")
                reset_in = seconds_until_reset(error)
                print(f"  #{n:<2} HTTP 429  retry-after: {retry_after}  (window resets in {reset_in:.0f}s)")
            else:
                print(f"  #{n:<2} HTTP 429")
            continue
        allowed += 1
        spent += float(raw.headers["x-litellm-response-cost"])
        print(f"  #{n:<2} HTTP 200")
    print(f"  → {allowed} of {QUICK_REQUESTS} allowed")
    return allowed, retry_after, reset_in, spent


def budget_part(client: openai.OpenAI, spent: float) -> tuple[Crossing | None, int, float]:
    """Returns (the request that crossed the budget, the request that was refused, spend)."""
    print(f"Part 2: budget, expensive requests until the gateway says no (budget ${BUDGET})")
    print(f"  spend so far: ${spent:.4f} (part 1)")
    crossing = None
    for n in range(1, 10):
        try:
            raw = client.chat.completions.with_raw_response.create(
                model="chat-small", max_tokens=20, messages=[{"role": "user", "content": LONG_PROMPT}]
            )
        except openai.RateLimitError as error:
            wait_for_window(seconds_until_reset(error))
            continue
        except openai.APIStatusError as error:
            if "Budget has been exceeded" not in str(error):
                raise
            print(f"  #{n}  HTTP {error.status_code}  Budget has been exceeded")
            return crossing, n, spent
        cost = float(raw.headers["x-litellm-response-cost"])
        crossed = spent < BUDGET <= spent + cost
        if crossed:
            crossing = Crossing(n, spent, spent + cost)
        note = f"  ← crossed ${BUDGET}, still allowed" if crossed else ""
        print(f"  #{n}  HTTP 200  ${spent:.4f} → ${spent + cost:.4f}{note}")
        spent += cost
    raise AssertionError("budget never enforced")


def main() -> None:
    lab.header(
        f"throwaway key, team payments-ai: {RPM_LIMIT} requests/min, ${BUDGET} budget (deleted at the end)",
        f"part 1 {QUICK_PROMPT!r} (max_tokens=1) · part 2 a {len(LONG_PROMPT)}-char refund policy "
        f"(max_tokens=20, ~$0.04 each):\n        {LONG_PROMPT[:90]}…",
    )
    with lab.temporary_key(
        "demo-limits", team="payments-ai", models=["chat-small"], rpm_limit=RPM_LIMIT, max_budget=BUDGET
    ) as key:
        client = lab.gateway(key)
        allowed, retry_after, reset_in, spent = rate_limit_part(client)
        wait_for_window(reset_in)
        crossing, rejected_at, spent = budget_part(client, spent)
        recorded = lab.admin().get("/key/info", params={"key": key}).json()["info"]["spend"]

    print(f"\nSpend, summed from x-litellm-response-cost headers: ${spent:.4f}")
    print(f"Spend in Postgres (/key/info) right now:            ${recorded:.4f}")

    crossed = (
        f"Part 2 request #{crossing.request} started under budget (${crossing.before:.4f}) and ended "
        f"over it (${crossing.after:.4f}), and it was allowed; request #{rejected_at} was refused. "
        if crossing
        else f"Part 2 request #{rejected_at} was refused. "
    )
    lab.summary(
        f"Rate limit: {allowed} of {QUICK_REQUESTS} requests got through; the rest got HTTP 429 with "
        f"retry-after: {retry_after}, i.e. back off and try again later.",
        f"Budget: {crossed}Spend is checked before a request and charged after it, so you can "
        "overspend by up to one request's cost: cap max_tokens or leave headroom. (Shown for "
        "requests one at a time; concurrent requests weren't tested.)",
        "The budget refusal is HTTP 422 'Budget has been exceeded', not 429: retrying won't help "
        "until someone raises the budget.",
        "Postgres lagged behind (spend is written in batches); the gateway enforces limits from "
        "its own counters in Valkey.",
    )


if __name__ == "__main__":
    main()
