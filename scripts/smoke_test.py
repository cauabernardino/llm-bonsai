"""End-to-end check of the gateway with a team key and the stock OpenAI SDK.

Uses the support-bot key: payments-ai's 3 rpm limit would trip on this many calls.
"""

import os
import sys
import time
from pathlib import Path

import openai
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GATEWAY_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
# support-bot may use chat-small and embed, but not chat-broken.
FORBIDDEN_MODEL = "chat-broken"


def check(name: str, fn) -> bool:
    start = time.perf_counter()
    try:
        detail = fn()
    except Exception as error:  # report every failure, then keep going
        print(f"FAIL  {name}: {type(error).__name__}: {error}")
        return False
    print(f"PASS  {name} ({time.perf_counter() - start:.1f}s): {detail}")
    return True


def main() -> None:
    key = os.environ.get("LITELLM_KEY_SUPPORT_BOT")
    if not key:
        sys.exit("LITELLM_KEY_SUPPORT_BOT missing from .env; run `make bootstrap` first")
    client = openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=key, max_retries=0)
    messages = [{"role": "user", "content": "Name one colour of the rainbow. One word."}]

    def chat():
        response = client.chat.completions.create(model="chat-small", messages=messages)
        text = response.choices[0].message.content
        assert text, "empty completion"
        return f"{text.strip()!r} via {response.model}, {response.usage.total_tokens} tokens"

    def stream():
        chunks = client.chat.completions.create(model="chat-small", messages=messages, stream=True)
        parts = [c.choices[0].delta.content or "" for c in chunks if c.choices]
        text = "".join(parts)
        assert text, "stream produced no content"
        return f"{len(parts)} chunks → {text.strip()!r}"

    def embed():
        response = client.embeddings.create(model="embed", input="refund policy")
        vector = response.data[0].embedding
        assert vector, "empty embedding"
        return f"{len(vector)}-dim vector"

    def forbidden():
        try:
            client.chat.completions.create(model=FORBIDDEN_MODEL, messages=messages)
        except openai.APIStatusError as error:
            return f"rejected with HTTP {error.status_code}"
        raise AssertionError(f"{FORBIDDEN_MODEL} was allowed for the support-bot key")

    results = [
        check("chat completion", chat),
        check("streaming", stream),
        check("embeddings", embed),
        check(f"forbidden model ({FORBIDDEN_MODEL})", forbidden),
    ]
    if not all(results):
        sys.exit(1)
    print("all smoke checks passed")


if __name__ == "__main__":
    main()
