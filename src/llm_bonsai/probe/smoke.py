"""End-to-end check of the gateway with a team key and the stock OpenAI SDK, and of the
traces it leaves in Langfuse.

Uses the support-bot key: payments-ai's 3 rpm limit would trip on this many calls.
Every request carries a per-run user id, which is how the Langfuse checks find this
run's traces.
"""

import time
from collections.abc import Callable
from typing import Any

import openai
from openai.types.chat import ChatCompletionMessageParam

from llm_bonsai import gateway, traces
from llm_bonsai.config import env

# support-bot may use chat-small and embed, but not chat-broken.
FORBIDDEN_MODEL = "chat-broken"
TEAM = "support-bot"
MESSAGES: list[ChatCompletionMessageParam] = [{"role": "user", "content": "Name one colour of the rainbow. One word."}]


def expect(condition: object, message: str) -> None:
    """A check that, unlike `assert`, survives `python -O`."""
    if not condition:
        raise AssertionError(message)


def check(name: str, probe: Callable[[], str]) -> bool:
    start = time.perf_counter()
    try:
        detail = probe()
    except Exception as error:  # report every failure, then keep going
        print(f"FAIL  {name}: {type(error).__name__}: {error}")
        return False
    print(f"PASS  {name} ({time.perf_counter() - start:.1f}s): {detail}")
    return True


def run() -> bool:
    """Run every check, printing PASS/FAIL for each; True if all passed."""
    gateway.require_gateway()
    client = gateway.client(env("LITELLM_KEY_SUPPORT_BOT"))
    user_id = traces.run_id("smoke")
    # `user` is the OpenAI field LiteLLM maps to the Langfuse user id; tags are free-form.
    metadata: dict[str, Any] = {"metadata": {"tags": ["smoke-test"]}}

    def chat() -> str:
        response = client.chat.completions.create(
            model="chat-small", messages=MESSAGES, user=user_id, extra_body=metadata
        )
        text = response.choices[0].message.content
        expect(text, "empty completion")
        tokens = response.usage.total_tokens if response.usage else "?"
        return f"{(text or '').strip()!r} via {response.model}, {tokens} tokens"

    def stream() -> str:
        chunks = client.chat.completions.create(
            model="chat-small", messages=MESSAGES, stream=True, user=user_id, extra_body=metadata
        )
        parts = [chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices]
        text = "".join(parts)
        expect(text, "stream produced no content")
        return f"{len(parts)} chunks → {text.strip()!r}"

    def embed() -> str:
        response = client.embeddings.create(model="embed", input="refund policy", user=user_id, extra_body=metadata)
        vector = response.data[0].embedding
        expect(vector, "empty embedding")
        return f"{len(vector)}-dim vector"

    def forbidden() -> str:
        try:
            client.chat.completions.create(model=FORBIDDEN_MODEL, messages=MESSAGES)
        except openai.APIStatusError as error:
            return f"rejected with HTTP {error.status_code}"
        raise AssertionError(f"{FORBIDDEN_MODEL} was allowed for the support-bot key")

    def traced() -> str:
        # chat + stream are both chat-small generations; wait for both so neither is missed.
        generations = traces.wait_for_generations(user_id, at_least=2)
        chat_gen = next(g for g in generations if g["model"] == "chat-small")
        tags = chat_gen["metadata"].get("attributes.langfuse.trace.tags", [])
        expect(f"user_api_key_team_alias:{TEAM}" in tags, f"team tag missing from {tags}")
        expect(chat_gen["totalUsage"] > 0, "no token usage")
        return (
            f"{len(generations)} generations for {user_id}; chat-small: "
            f"{chat_gen['totalUsage']} tokens, {chat_gen['latency']:.2f}s, "
            f"${chat_gen['totalCost']}, team tag present"
        )

    def failure_traced() -> str:
        # Only the master key may call chat-broken with fallbacks off, i.e. force a real
        # upstream failure. Langfuse should record it as an ERROR-level generation.
        admin = gateway.client(env("LITELLM_MASTER_KEY"))
        try:
            admin.chat.completions.create(
                model="chat-broken", messages=MESSAGES, user=user_id, extra_body={"disable_fallbacks": True}
            )
        except openai.APIStatusError:
            pass
        else:
            raise AssertionError("chat-broken unexpectedly succeeded")
        failed = traces.wait_for_generations(user_id, level="ERROR")[0]
        return f"ERROR generation: {failed['statusMessage'][:70]}…"

    results = [
        check("chat completion", chat),
        check("streaming", stream),
        check("embeddings", embed),
        check(f"forbidden model ({FORBIDDEN_MODEL})", forbidden),
        check("traced in Langfuse", traced),
        check("failed call traced in Langfuse", failure_traced),
    ]
    if all(results):
        print("all smoke checks passed")
    return all(results)
