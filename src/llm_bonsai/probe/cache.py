"""Send the same prompt through the gateway's Valkey-backed response cache, four ways.

The cache is opt-in (cache_params.mode: default_off): a request uses it only when it
sends {"cache": {"use-cache": true}}. A random run id in the prompt makes the first
request a guaranteed miss.
"""

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import openai
from rich.text import Text

from llm_bonsai import report
from llm_bonsai.catalog import CHAT_SMALL, SUPPORT_BOT
from llm_bonsai.lab import Lab


@dataclass(frozen=True)
class Request:
    label: str
    use_cache: bool
    temperature: float | None = None  # None: the model's default


@dataclass(frozen=True)
class Reply:
    elapsed: float
    headers: Mapping[str, str]  # case-insensitive: the SDK hands over its own Headers
    answer: str

    @property
    def hit(self) -> bool:
        return "x-litellm-cache-key" in self.headers

    @property
    def cost(self) -> float:
        return float(self.headers.get("x-litellm-response-cost", 0))


REQUESTS = [
    Request("opt in", use_cache=True),
    Request("opt in, same as #1", use_cache=True),
    Request("opt in, temperature=0.1", use_cache=True, temperature=0.1),
    Request("no opt-in", use_cache=False),
]


def send(client: openai.OpenAI, prompt: str, request: Request) -> Reply:
    start = time.perf_counter()
    raw = client.chat.completions.with_raw_response.create(
        model=CHAT_SMALL,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"cache": {"use-cache": request.use_cache}},
        temperature=openai.omit if request.temperature is None else request.temperature,
    )
    elapsed = time.perf_counter() - start
    answer = (raw.parse().choices[0].message.content or "").strip().replace("\n", " / ")
    return Reply(elapsed, raw.headers, answer)


def takeaways(first: Reply, repeat: Reply, changed: Reply, skipped: Reply) -> list[str]:
    if not repeat.hit:
        return [
            "#2 was a cache miss too, so the cache isn't working: check that Valkey is running and "
            "cache_params in src/llm_bonsai/infra/litellm/values.yaml."
        ]
    identical = "word for word #1's answer" if repeat.answer == first.answer else "yet its answer differs from #1's"
    bullets = [
        f"#2 came from Valkey in {repeat.elapsed * 1000:.0f} ms instead of {first.elapsed * 1000:.0f} ms "
        f"(~{first.elapsed / max(repeat.elapsed, 1e-3):.0f}× faster), cost ${repeat.cost:.4f}, and is {identical}: "
        "the model never ran.",
        "#3 changed only the temperature and missed: the cache key covers model, messages and parameters. "
        "This is exact-match caching."
        if not changed.hit
        else "#3 changed only the temperature and still hit, so this gateway leaves temperature out of the cache key.",
    ]
    if not skipped.hit and skipped.cost > 0:
        bullets.append(
            "#4 didn't opt in, so it went to the model (mode: default_off). Caching stays a per-request "
            "decision: right for repeatable answers, wrong for anything that should vary."
        )
    return bullets


def run(lab: Lab) -> None:
    lab.gateway.require()
    client = lab.gateway.client(lab.env.get(SUPPORT_BOT.env_var))
    prompt = f"Write a haiku about a bonsai tree. (run {uuid.uuid4().hex[:6]})"
    report.header(
        "support-bot (long-lived key from `llm-bonsai setup teams`)",
        f"{prompt!r} (the random run id makes request #1 a guaranteed cache miss)",
    )

    with report.console.status("sending 4 requests…"):
        replies = [send(client, prompt, request) for request in REQUESTS]
    requests = report.table(report.right("#"), "request", report.right("took"), "cache", report.right("cost"))
    for n, (request, reply) in enumerate(zip(REQUESTS, replies, strict=True), start=1):
        if not request.use_cache:
            cache = Text("off", style="dim")
        else:
            cache = Text("HIT", style="bold green") if reply.hit else Text("miss", style="yellow")
        requests.add_row(str(n), request.label, f"{reply.elapsed * 1000:.0f} ms", cache, f"${reply.cost:.4f}")
    report.console.print(requests)

    first, repeat, changed, skipped = replies
    source = "cache" if repeat.hit else "model"
    marker = ("identical to #1", "green") if repeat.answer == first.answer else ("differs from #1", "yellow")
    report.line(("Answer #1 (model): ", "bold"), first.answer)
    report.line((f"Answer #2 ({source}): ", "bold"), repeat.answer, "  ← ", marker)
    report.line((f"Answer #3 ({'cache' if changed.hit else 'model'}): ", "bold"), changed.answer)
    report.line(("Answer #4 (model): ", "bold"), skipped.answer)

    report.summary(
        *takeaways(first, repeat, changed, skipped),
        not_shown="other gateway replicas reading the same entries (they share this Valkey), and "
        "semantic caching, which matches paraphrases by embedding similarity at the risk of "
        "answering a question that only looks similar.",
    )
