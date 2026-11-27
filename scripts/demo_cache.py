"""Send the same prompt through the gateway's Valkey-backed response cache, four ways.

The cache is opt-in (cache_params.mode: default_off): a request uses it only when it
sends {"cache": {"use-cache": true}}. A random run id in the prompt makes the first
request a guaranteed miss.
"""

import time
import uuid

import lab

# (label, use the cache?, extra parameters)
REQUESTS = [
    ("opt in", True, {}),
    ("opt in, same as #1", True, {}),
    ("opt in, temperature=0.1", True, {"temperature": 0.1}),
    ("no opt-in", False, {}),
]


def timed_request(client, prompt: str, use_cache: bool, params: dict):
    start = time.perf_counter()
    raw = client.chat.completions.with_raw_response.create(
        model="chat-small",
        messages=[{"role": "user", "content": prompt}],
        extra_body={"cache": {"use-cache": use_cache}},
        **params,
    )
    return time.perf_counter() - start, raw


def one_line(raw) -> str:
    return raw.parse().choices[0].message.content.strip().replace("\n", " / ")


def main() -> None:
    lab.require_gateway()
    prompt = f"Write a haiku about a bonsai tree. (run {uuid.uuid4().hex[:6]})"
    lab.header(
        "support-bot (long-lived key from `make bootstrap`)",
        f"{prompt!r}\n        the random run id makes request #1 a guaranteed cache miss",
    )
    client = lab.gateway(lab.env("LITELLM_KEY_SUPPORT_BOT"))

    results = [timed_request(client, prompt, use_cache, params) for _, use_cache, params in REQUESTS]
    print(f" #  {'request':<26} {'took':>9}  cache  cost")
    for n, ((label, use_cache, _), (elapsed, raw)) in enumerate(zip(REQUESTS, results), start=1):
        cache = ("HIT " if "x-litellm-cache-key" in raw.headers else "miss") if use_cache else "off "
        cost = float(raw.headers.get("x-litellm-response-cost", 0))
        print(f" {n}  {label:<26} {elapsed * 1000:6.0f} ms  {cache}   ${cost:.4f}")

    first, cached, fresh = one_line(results[0][1]), one_line(results[1][1]), one_line(results[3][1])
    print(f"\nAnswer #1 (model): {first}")
    print(f"Answer #2 (cache): {cached}  {'← identical to #1' if cached == first else '← differs from #1!'}")
    print(f"Answer #4 (model): {fresh}")

    timings = [elapsed for elapsed, _ in results]
    lab.summary(
        f"#2 came from Valkey in {timings[1] * 1000:.0f} ms instead of {timings[0] * 1000:.0f} ms "
        f"(~{timings[0] / max(timings[1], 1e-3):.0f}× faster) and cost $0: the model never ran, and the "
        "answer is #1's word for word.",
        "#3 changed only the temperature and missed: the cache key covers model, messages and "
        "parameters. This is exact-match caching.",
        "#4 didn't opt in, so it went to the model (mode: default_off). Caching stays a per-request "
        "decision: right for repeatable answers, wrong for anything that should vary.",
        not_shown="other gateway replicas reading the same entries (they share this Valkey), and "
        "semantic caching, which matches paraphrases by embedding similarity at the risk of "
        "answering a question that only looks similar.",
    )


if __name__ == "__main__":
    main()
