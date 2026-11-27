"""Shared plumbing for the demo scripts: .env, clients, throwaway keys, output conventions.

Output conventions, so every demo reads the same way:
- `header()` first: which key the demo runs as and the prompt it sends;
- statuses as `HTTP 200` / `HTTP 429` / `HTTP 422`;
- `summary()` last: what *this run* showed, plus clearly labelled "not shown here" points.
"""

import os
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import openai
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GATEWAY_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LANGFUSE_URL = os.environ.get("LANGFUSE_URL", "http://localhost:3000")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} missing from .env; run `make up` and `make bootstrap` first")
    return value


def require_gateway(wait_s: float = 90) -> None:
    """Exit with a clear message unless the gateway answers.

    After a redeploy the port-forward needs a moment to reattach (`make pf` restarts it),
    and it may briefly sit on a pod that is shutting down and never answers. Probe with a
    short timeout so one hung request can't use up the whole wait.
    """
    deadline = time.monotonic() + wait_s
    waiting = False
    while True:
        try:
            httpx.get(f"{GATEWAY_URL}/health/liveliness", timeout=3).raise_for_status()
            return
        except httpx.HTTPError as error:
            if not waiting:
                print(f"  (gateway at {GATEWAY_URL} not answering yet; waiting up to {wait_s:.0f}s, needs `make pf`)", flush=True)
                waiting = True
            if time.monotonic() > deadline:
                sys.exit(f"Gateway not reachable at {GATEWAY_URL} ({type(error).__name__}). Is `make pf` running?")
            time.sleep(1)


def gateway(key: str) -> openai.OpenAI:
    """OpenAI SDK pointed at the gateway. No SDK retries: the demos show raw behaviour."""
    return openai.OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=key, max_retries=0)


def admin() -> httpx.Client:
    """Management API client authenticated with the master key."""
    require_gateway()
    return httpx.Client(
        base_url=GATEWAY_URL,
        headers={"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"},
        timeout=30,
    )


def team_id(client: httpx.Client, alias: str) -> str:
    teams = client.get("/team/list").raise_for_status().json()
    return next(t["team_id"] for t in teams if t["team_alias"] == alias)


@contextmanager
def temporary_key(purpose: str, team: str, **limits) -> Generator[str, None, None]:
    """A fresh virtual key for one demo run, deleted afterwards.

    Demos that burn through budgets or rate limits get their own key, so re-running
    them never depends on (or exhausts) the long-lived keys from `make bootstrap`.
    """
    client = admin()
    response = client.post(
        "/key/generate",
        json={
            "team_id": team_id(client, team),
            "key_alias": f"{purpose}-{uuid.uuid4().hex[:6]}",
            **limits,
        },
    ).raise_for_status()
    key = response.json()["key"]
    try:
        yield key
    finally:
        client.post("/key/delete", json={"keys": [key]})


def wait_for_generations(user_id: str, at_least: int = 1, deadline_s: float = 60, **filters) -> list[dict]:
    """Poll Langfuse until `at_least` generations by `user_id` exist; ingestion is asynchronous.

    Langfuse v4 serves reads from /api/public/v2/observations. Its `tags` filter is
    ignored, so runs are told apart by a unique user id instead.
    """
    params = {
        "type": "GENERATION",
        "userId": user_id,
        "fields": "core,basic,usage,model,metadata",
        **filters,
    }
    auth = (env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY"))
    with httpx.Client(base_url=LANGFUSE_URL, auth=auth, timeout=10) as client:
        stop = time.monotonic() + deadline_s
        while time.monotonic() < stop:
            found = client.get("/api/public/v2/observations", params=params).raise_for_status()
            data = found.json()["data"]
            if len(data) >= at_least:
                return data
            time.sleep(2)
    raise TimeoutError(f"fewer than {at_least} generations in Langfuse for user {user_id} after {deadline_s}s")


def run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def header(key: str, prompt: str) -> None:
    print(f"Key:    {key}")
    print(f"Prompt: {prompt}\n")


def summary(*shown: str, not_shown: str | None = None) -> None:
    print("\nWhat you just saw")
    print("─" * 17)
    for point in shown:
        print(f"• {point}")
    if not_shown:
        print(f"• Not shown here: {not_shown}")
