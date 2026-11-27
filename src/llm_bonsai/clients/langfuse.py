"""Langfuse's public API, for finding a run's traces again. Reached through a port-forward opened on first use."""

import time
import uuid

import httpx

from llm_bonsai.catalog import LANGFUSE
from llm_bonsai.clients.tunnels import Tunnels
from llm_bonsai.config import EnvFile
from llm_bonsai.models import Generation


def run_id(prefix: str) -> str:
    """A unique user id for one run; it is how a run's traces are found again."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Langfuse:
    def __init__(self, tunnels: Tunnels, env: EnvFile) -> None:
        self._tunnels = tunnels
        self._env = env

    def wait_for_generations(
        self, user_id: str, at_least: int = 1, deadline_s: float = 60, **filters: str
    ) -> list[Generation]:
        """Poll until `at_least` generations by `user_id` exist; ingestion is asynchronous.

        Langfuse v4 serves reads from /api/public/v2/observations. Its `tags` filter is
        ignored, so runs are told apart by a unique user id instead.
        """
        params = {"type": "GENERATION", "userId": user_id, "fields": "core,basic,usage,model,metadata", **filters}
        auth = (self._env.get("LANGFUSE_PUBLIC_KEY"), self._env.get("LANGFUSE_SECRET_KEY"))
        with httpx.Client(base_url=self._tunnels.url(LANGFUSE), auth=auth, timeout=10) as client:
            stop = time.monotonic() + deadline_s
            while time.monotonic() < stop:
                found = client.get("/api/public/v2/observations", params=params).raise_for_status()
                data = found.json()["data"]
                if len(data) >= at_least:
                    return [Generation.from_api(generation) for generation in data]
                time.sleep(2)
        raise TimeoutError(f"fewer than {at_least} generations in Langfuse for user {user_id} after {deadline_s:.0f}s")
