"""Langfuse access: finding a run's traces again, reached through a port-forward opened on first use."""

import time
import uuid
from typing import Any

import httpx

from llm_bonsai import tunnels
from llm_bonsai.config import env


def run_id(prefix: str) -> str:
    """A unique user id for one run; it is how a run's traces are found again."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def wait_for_generations(
    user_id: str, at_least: int = 1, deadline_s: float = 60, **filters: str
) -> list[dict[str, Any]]:
    """Poll Langfuse until `at_least` generations by `user_id` exist; ingestion is asynchronous.

    Langfuse v4 serves reads from /api/public/v2/observations. Its `tags` filter is
    ignored, so runs are told apart by a unique user id instead.
    """
    params = {"type": "GENERATION", "userId": user_id, "fields": "core,basic,usage,model,metadata", **filters}
    auth = (env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY"))
    with httpx.Client(base_url=tunnels.url(tunnels.LANGFUSE), auth=auth, timeout=10) as client:
        stop = time.monotonic() + deadline_s
        while time.monotonic() < stop:
            found = client.get("/api/public/v2/observations", params=params).raise_for_status()
            data: list[dict[str, Any]] = found.json()["data"]
            if len(data) >= at_least:
                return data
            time.sleep(2)
    raise TimeoutError(f"fewer than {at_least} generations in Langfuse for user {user_id} after {deadline_s:.0f}s")
