"""Hit a key's rate limit, then spend through its budget.

Runs on a throwaway key with the payments-ai limits (3 requests/minute, $0.05 budget),
so it can be re-run any time without exhausting the long-lived payments-ai key.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import openai

from llm_bonsai import report
from llm_bonsai.catalog import CHAT_SMALL, PAYMENTS_AI
from llm_bonsai.errors import LabError
from llm_bonsai.lab import Lab

RPM_LIMIT = 3
BUDGET = 0.05
QUICK_REQUESTS = 10
QUICK_PROMPT = "hi"
# ~425 prompt tokens: at the fake $0.0001/token, each request costs ~$0.04.
LONG_PROMPT = "Summarise this refund policy in one sentence. " + (
    "Customers may return items within thirty days for a full refund. " * 30
)
DEFAULT_WINDOW_S = 60.0
MAX_BUDGET_REQUESTS = 9


@dataclass(frozen=True)
class RateLimitResult:
    allowed: int
    retry_after: str | None  # from the first 429, if any
    reset_in: float
    spent: float


@dataclass(frozen=True)
class Crossing:
    """The request that started under the budget and ended over it."""

    request: int
    before: float
    after: float


@dataclass(frozen=True)
class BudgetResult:
    crossing: Crossing | None
    refused_at: int
    status: int
    spent: float


def seconds_until_reset(message: str, now: datetime) -> float:
    """LiteLLM's 429 says when the window resets: 'Limit resets at: 2026-11-27 20:45:56 UTC'."""
    found = re.search(r"Limit resets at: ([\d-]+ [\d:]+) UTC", message)
    if not found:
        return DEFAULT_WINDOW_S
    reset_at = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return max(0.0, (reset_at - now).total_seconds()) + 1


def wait_for_window(seconds: float) -> None:
    print(f"\n… waiting {seconds:.0f}s for the rate-limit window to reset\n")
    time.sleep(seconds)


def rate_limit_part(client: openai.OpenAI) -> RateLimitResult:
    print(f"Part 1: rate limit, {QUICK_REQUESTS} requests in a row")
    allowed, retry_after, reset_in, spent = 0, None, DEFAULT_WINDOW_S, 0.0
    limited = False
    for n in range(1, QUICK_REQUESTS + 1):
        try:
            raw = client.chat.completions.with_raw_response.create(
                model=CHAT_SMALL, max_tokens=1, messages=[{"role": "user", "content": QUICK_PROMPT}]
            )
        except openai.RateLimitError as error:
            if not limited:
                limited = True
                retry_after = error.response.headers.get("retry-after")
                reset_in = seconds_until_reset(str(error), datetime.now(UTC))
                print(f"  #{n:<2} HTTP 429  retry-after: {retry_after}  (window resets in {reset_in:.0f}s)")
            else:
                print(f"  #{n:<2} HTTP 429")
            continue
        allowed += 1
        spent += float(raw.headers["x-litellm-response-cost"])
        print(f"  #{n:<2} HTTP 200")
    print(f"  → {allowed} of {QUICK_REQUESTS} allowed")
    return RateLimitResult(allowed, retry_after, reset_in if limited else 0.0, spent)


def budget_part(client: openai.OpenAI, spent: float) -> BudgetResult:
    print(f"Part 2: budget, expensive requests until the gateway says no (budget ${BUDGET})")
    print(f"  spend so far: ${spent:.4f} (part 1)")
    crossing = None
    for n in range(1, MAX_BUDGET_REQUESTS + 1):
        try:
            raw = client.chat.completions.with_raw_response.create(
                model=CHAT_SMALL, max_tokens=20, messages=[{"role": "user", "content": LONG_PROMPT}]
            )
        except openai.RateLimitError as error:
            wait_for_window(seconds_until_reset(str(error), datetime.now(UTC)))
            continue
        except openai.APIStatusError as error:
            if "Budget has been exceeded" not in str(error):
                raise
            print(f"  #{n}  HTTP {error.status_code}  Budget has been exceeded")
            return BudgetResult(crossing, n, error.status_code, spent)
        cost = float(raw.headers["x-litellm-response-cost"])
        crossed = spent < BUDGET <= spent + cost
        if crossed:
            crossing = Crossing(n, spent, spent + cost)
        note = f"  ← crossed ${BUDGET}, still allowed" if crossed else ""
        print(f"  #{n}  HTTP 200  ${spent:.4f} → ${spent + cost:.4f}{note}")
        spent += cost
    raise LabError(f"the gateway never enforced the ${BUDGET} budget in {MAX_BUDGET_REQUESTS} requests")


def takeaways(quick: RateLimitResult, budget: BudgetResult, recorded: float) -> list[str]:
    if quick.allowed < QUICK_REQUESTS:
        backoff = f" with retry-after: {quick.retry_after}" if quick.retry_after else ""
        rate = (
            f"Rate limit: {quick.allowed} of {QUICK_REQUESTS} requests got through; the rest got HTTP 429{backoff}, "
            "i.e. back off and try again later."
        )
    else:
        rate = (
            f"Rate limit: all {QUICK_REQUESTS} requests got through, so the {RPM_LIMIT}/minute limit wasn't enforced."
        )
    if budget.crossing:
        crossing = budget.crossing
        spend = (
            f"Budget: part 2 request #{crossing.request} started under budget (${crossing.before:.4f}) and ended "
            f"over it (${crossing.after:.4f}), and it was allowed; request #{budget.refused_at} was refused. "
            "Spend is checked before a request and charged after it, so you can overspend by up to one "
            "request's cost: cap max_tokens or leave headroom. (Shown for requests one at a time; concurrent "
            "requests weren't tested.)"
        )
    else:
        spend = f"Budget: part 2 request #{budget.refused_at} was refused."
    bullets = [
        rate,
        spend,
        f"The budget refusal is HTTP {budget.status} 'Budget has been exceeded', not 429: retrying won't help "
        "until someone raises the budget.",
    ]
    if recorded < budget.spent - 1e-6:
        bullets.append(
            f"Postgres still showed ${recorded:.4f} of ${budget.spent:.4f}: spend is written in batches, so the "
            "gateway enforced the budget from its own counters, not from Postgres."
        )
    return bullets


def run(lab: Lab) -> None:
    lab.gateway.require()
    report.header(
        f"throwaway key, team {PAYMENTS_AI.alias}: {RPM_LIMIT} requests/min, ${BUDGET} budget (deleted at the end)",
        f"part 1 {QUICK_PROMPT!r} (max_tokens=1) · part 2 a {len(LONG_PROMPT)}-char refund policy "
        f"(max_tokens=20, ~$0.04 each):\n        {LONG_PROMPT[:90]}…",
    )
    with lab.gateway.temporary_key(
        "probe-limits", team=PAYMENTS_AI.alias, models=[CHAT_SMALL], rpm_limit=RPM_LIMIT, max_budget=BUDGET
    ) as key:
        client = lab.gateway.client(key)
        quick = rate_limit_part(client)
        if quick.reset_in:
            wait_for_window(quick.reset_in)
        budget = budget_part(client, quick.spent)
        with lab.gateway.admin() as admin:
            recorded = admin.key_spend(key)

    print(f"\nSpend, summed from x-litellm-response-cost headers: ${budget.spent:.4f}")
    print(f"Spend in Postgres (/key/info) right now:            ${recorded:.4f}")
    report.summary(*takeaways(quick, budget, recorded))
