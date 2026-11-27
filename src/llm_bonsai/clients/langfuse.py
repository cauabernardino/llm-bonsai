"""Langfuse: the public API for reading traces back, and the Python SDK for writing them.

Both go through a port-forward opened on first use. The SDK is built on first use too,
and shut down (flushing what it buffered) before that port-forward closes.
"""

import time
import urllib.parse
import uuid
from collections.abc import Callable

import httpx
import langfuse as lf

from llm_bonsai.catalog import LANGFUSE, LANGFUSE_PROJECT_ID, LANGFUSE_UI_URL
from llm_bonsai.clients.tunnels import Tunnels
from llm_bonsai.config import EnvFile
from llm_bonsai.models import Observation

FIELDS = "core,basic,usage,model,prompt,metadata"


def run_id(prefix: str) -> str:
    """A unique user id for one run; it is how a run's traces are found again."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def trace_url(trace_id: str) -> str:
    """The trace in the browser, served by `llm-bonsai ui`."""
    return f"{LANGFUSE_UI_URL}/project/{LANGFUSE_PROJECT_ID}/traces/{trace_id}"


def experiments_url(baseline: str, *compared: str) -> str:
    """One experiment's results, or several side by side (the UI's own `baseline` + `c` parameters)."""
    query = urllib.parse.urlencode([("baseline", baseline), *(("c", other) for other in compared)])
    return f"{LANGFUSE_UI_URL}/project/{LANGFUSE_PROJECT_ID}/experiments/results?{query}"


class Langfuse:
    def __init__(self, tunnels: Tunnels, env: EnvFile) -> None:
        self._tunnels = tunnels
        self._env = env
        self._sdk: lf.Langfuse | None = None

    @property
    def base_url(self) -> str:
        return self._tunnels.url(LANGFUSE)

    @property
    def sdk(self) -> lf.Langfuse:
        if self._sdk is None:
            # Everything explicit: the SDK would otherwise read LANGFUSE_*/OTEL_* settings from the shell.
            self._sdk = lf.Langfuse(
                public_key=self._env.get("LANGFUSE_PUBLIC_KEY"),
                secret_key=self._env.get("LANGFUSE_SECRET_KEY"),
                base_url=self.base_url,
                tracing_enabled=True,
                sample_rate=1.0,
                environment="default",
                timeout=10,
            )
        return self._sdk

    def browser_url(self, url: str) -> str:
        """An SDK-made URL (which points at this command's random tunnel port) as `llm-bonsai ui` serves it."""
        return url.replace(self.base_url, LANGFUSE_UI_URL, 1)

    def close(self) -> None:
        if self._sdk is not None:
            self._sdk.shutdown()
            self._sdk = None

    def observations(self, **params: str) -> list[Observation]:
        """One read of /api/public/v2/observations, newest first."""
        auth = (self._env.get("LANGFUSE_PUBLIC_KEY"), self._env.get("LANGFUSE_SECRET_KEY"))
        with httpx.Client(base_url=self.base_url, auth=auth, timeout=10) as client:
            found = client.get("/api/public/v2/observations", params={"fields": FIELDS, **params})
        return [Observation.from_api(data) for data in found.raise_for_status().json()["data"]]

    def _poll(
        self, params: dict[str, str], done: Callable[[list[Observation]], bool], deadline_s: float
    ) -> list[Observation]:
        """Poll until `done(observations)`; ingestion is asynchronous. Returns the last read either way."""
        stop = time.monotonic() + deadline_s
        while True:
            observations = self.observations(**params)
            if done(observations) or time.monotonic() > stop:
                return observations
            time.sleep(2)

    def wait_for_generations(
        self, user_id: str, at_least: int = 1, deadline_s: float = 60, **filters: str
    ) -> list[Observation]:
        """Poll until `at_least` generations by `user_id` exist, or raise TimeoutError.

        Langfuse v4 serves reads from /api/public/v2/observations. Its `tags` filter is
        ignored, so runs are told apart by a unique user id instead.
        """
        params = {"type": "GENERATION", "userId": user_id, **filters}
        found = self._poll(params, lambda observations: len(observations) >= at_least, deadline_s)
        if len(found) < at_least:
            raise TimeoutError(
                f"fewer than {at_least} generations in Langfuse for user {user_id} after {deadline_s:.0f}s"
            )
        return found

    def wait_for_trace(
        self, trace_id: str, done: Callable[[list[Observation]], bool], deadline_s: float = 60
    ) -> list[Observation]:
        """Every observation in a trace, once `done` says all the expected ones arrived (or the deadline passed)."""
        return self._poll({"traceId": trace_id}, done, deadline_s)
