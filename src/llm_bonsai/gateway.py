"""Clients for the LiteLLM gateway: the OpenAI SDK for traffic, httpx for its management API.

The gateway is reached through a port-forward opened on first use (see `tunnels`).
"""

import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import httpx
import openai

from llm_bonsai import tunnels
from llm_bonsai.config import env
from llm_bonsai.errors import LabError

# A port-forward pinned to a pod that is shutting down accepts connections but never
# answers; after this long without an answer, reconnect to pick a live pod.
RECONNECT_AFTER_S = 5


def base_url() -> str:
    return tunnels.url(tunnels.GATEWAY)


def require_gateway(wait_s: float = 10, health: str = "liveliness") -> None:
    """Return once the gateway answers its health check, or raise.

    `health="readiness"` also waits for the gateway's database, which the management API needs.
    """
    tunnel = tunnels.forward(tunnels.GATEWAY)
    start = time.monotonic()
    last_reconnect = start
    waiting = False
    while True:
        try:
            httpx.get(f"{tunnel.url}/health/{health}", timeout=3).raise_for_status()
            return
        except httpx.HTTPError as error:
            now = time.monotonic()
            if now - start > wait_s:
                raise LabError(
                    f"the gateway didn't answer within {wait_s:.0f}s ({type(error).__name__}); "
                    "check `kubectl -n llm get pods`"
                ) from error
            if not waiting:
                print(f"  (waiting up to {wait_s:.0f}s for the gateway to answer)", flush=True)
                waiting = True
            if now - last_reconnect > RECONNECT_AFTER_S:
                tunnel.restart()
                last_reconnect = now
            time.sleep(1)


def client(key: str) -> openai.OpenAI:
    """OpenAI SDK pointed at the gateway. No SDK retries: the probes show raw behaviour."""
    return openai.OpenAI(base_url=f"{base_url()}/v1", api_key=key, max_retries=0)


def admin() -> httpx.Client:
    """Management API client authenticated with the master key."""
    master_key = env("LITELLM_MASTER_KEY")
    require_gateway(health="readiness")
    return httpx.Client(base_url=base_url(), headers={"Authorization": f"Bearer {master_key}"}, timeout=30)


def team_id(api: httpx.Client, alias: str) -> str:
    teams: list[dict[str, Any]] = api.get("/team/list").raise_for_status().json()
    for team in teams:
        if team["team_alias"] == alias:
            return str(team["team_id"])
    raise LabError(f"team {alias!r} not found; run `llm-bonsai setup teams`")


@contextmanager
def temporary_key(purpose: str, team: str, **limits: Any) -> Generator[str, None, None]:
    """A fresh virtual key for one probe run, deleted afterwards.

    Probes that burn through budgets or rate limits get their own key, so re-running
    them never depends on (or exhausts) the long-lived keys from `setup teams`.
    """
    with admin() as api:
        response = api.post(
            "/key/generate",
            json={"team_id": team_id(api, team), "key_alias": f"{purpose}-{uuid.uuid4().hex[:6]}", **limits},
        ).raise_for_status()
        key: str = response.json()["key"]
        try:
            yield key
        finally:
            api.post("/key/delete", json={"keys": [key]})
