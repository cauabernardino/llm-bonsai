"""End-to-end check of the gateway with a team key and the stock OpenAI SDK, and of the
traces it leaves in Langfuse.

Uses the support-bot key: payments-ai's 3 rpm limit would trip on this many calls.
Every request carries a per-run user id, which is how the Langfuse checks find this
run's traces.
"""

import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import openai
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GATEWAY_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LANGFUSE_URL = os.environ.get("LANGFUSE_URL", "http://localhost:3000")
# support-bot may use chat-small and embed, but not chat-broken.
FORBIDDEN_MODEL = "chat-broken"
TEAM = "support-bot"
TRACE_DEADLINE_S = 60


def check(name: str, fn) -> bool:
    start = time.perf_counter()
    try:
        detail = fn()
    except Exception as error:  # report every failure, then keep going
        print(f"FAIL  {name}: {type(error).__name__}: {error}")
        return False
    print(f"PASS  {name} ({time.perf_counter() - start:.1f}s): {detail}")
    return True


def wait_for_generations(langfuse: httpx.Client, user_id: str, **filters) -> list[dict]:
    """Poll Langfuse until generations for `user_id` show up; ingestion is asynchronous."""
    params = {
        "type": "GENERATION",
        "userId": user_id,
        "fields": "core,basic,usage,model,metadata",
        **filters,
    }
    deadline = time.monotonic() + TRACE_DEADLINE_S
    while time.monotonic() < deadline:
        found = langfuse.get("/api/public/v2/observations", params=params).raise_for_status()
        if data := found.json()["data"]:
            return data
        time.sleep(2)
    raise AssertionError(f"nothing in Langfuse for user {user_id} {filters} after {TRACE_DEADLINE_S}s")


def main() -> None:
    key = os.environ.get("LITELLM_KEY_SUPPORT_BOT")
    if not key:
        sys.exit("LITELLM_KEY_SUPPORT_BOT missing from .env; run `make bootstrap` first")
    client = openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=key, max_retries=0)
    langfuse = httpx.Client(
        base_url=LANGFUSE_URL,
        auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]),
        timeout=10,
    )

    user_id = f"smoke-{uuid.uuid4().hex[:8]}"
    # `user` is the OpenAI field LiteLLM maps to the Langfuse user id; tags are free-form.
    tracing = {"user": user_id, "extra_body": {"metadata": {"tags": ["smoke-test"]}}}
    messages = [{"role": "user", "content": "Name one colour of the rainbow. One word."}]

    def chat():
        response = client.chat.completions.create(model="chat-small", messages=messages, **tracing)
        text = response.choices[0].message.content
        assert text, "empty completion"
        return f"{text.strip()!r} via {response.model}, {response.usage.total_tokens} tokens"

    def stream():
        chunks = client.chat.completions.create(
            model="chat-small", messages=messages, stream=True, **tracing
        )
        parts = [c.choices[0].delta.content or "" for c in chunks if c.choices]
        text = "".join(parts)
        assert text, "stream produced no content"
        return f"{len(parts)} chunks → {text.strip()!r}"

    def embed():
        response = client.embeddings.create(model="embed", input="refund policy", **tracing)
        vector = response.data[0].embedding
        assert vector, "empty embedding"
        return f"{len(vector)}-dim vector"

    def forbidden():
        try:
            client.chat.completions.create(model=FORBIDDEN_MODEL, messages=messages)
        except openai.APIStatusError as error:
            return f"rejected with HTTP {error.status_code}"
        raise AssertionError(f"{FORBIDDEN_MODEL} was allowed for the support-bot key")

    def traced():
        generations = wait_for_generations(langfuse, user_id)
        chat_gen = next(g for g in generations if g["model"] == "chat-small")
        tags = chat_gen["metadata"].get("attributes.langfuse.trace.tags", [])
        assert f"user_api_key_team_alias:{TEAM}" in tags, f"team tag missing from {tags}"
        assert chat_gen["totalUsage"] > 0, "no token usage"
        return (
            f"{len(generations)} generations for {user_id}; chat-small: "
            f"{chat_gen['totalUsage']} tokens, {chat_gen['latency']:.2f}s, "
            f"${chat_gen['totalCost']}, team tag present"
        )

    def failure_traced():
        # Only the master key may call chat-broken with fallbacks off, i.e. force a real
        # upstream failure. Langfuse should record it as an ERROR-level generation.
        admin = openai.OpenAI(
            base_url=f"{GATEWAY_URL}/v1", api_key=os.environ["LITELLM_MASTER_KEY"], max_retries=0
        )
        try:
            admin.chat.completions.create(
                model="chat-broken",
                messages=messages,
                user=user_id,
                extra_body={"disable_fallbacks": True},
            )
        except openai.APIStatusError:
            pass
        else:
            raise AssertionError("chat-broken unexpectedly succeeded")
        failed = wait_for_generations(langfuse, user_id, level="ERROR")[0]
        return f"ERROR generation: {failed['statusMessage'][:70]}…"

    results = [
        check("chat completion", chat),
        check("streaming", stream),
        check("embeddings", embed),
        check(f"forbidden model ({FORBIDDEN_MODEL})", forbidden),
        check("traced in Langfuse", traced),
        check("failed call traced in Langfuse", failure_traced),
    ]
    if not all(results):
        sys.exit(1)
    print("all smoke checks passed")


if __name__ == "__main__":
    main()
