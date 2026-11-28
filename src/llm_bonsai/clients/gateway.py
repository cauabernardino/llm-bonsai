"""The LiteLLM gateway: the OpenAI SDK for traffic, httpx for its management API.

It is reached through a port-forward opened on first use (see `tunnels`).
"""

import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import httpx
import openai

from llm_bonsai.catalog import GATEWAY, GATEWAY_POD_LABELS
from llm_bonsai.clients.kube import Kube
from llm_bonsai.clients.tunnels import Tunnels
from llm_bonsai.config import EnvFile
from llm_bonsai.errors import LabError
from llm_bonsai.models import GatewayPod

# A port-forward pinned to a pod that is shutting down accepts connections but never
# answers; after this long without an answer, reconnect to pick a live pod.
RECONNECT_AFTER_S = 5


class GatewayAdmin:
    """LiteLLM's management API (teams and virtual keys), authenticated with the master key."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def find_team(self, alias: str) -> str | None:
        teams: list[dict[str, Any]] = self._http.get("/team/list").raise_for_status().json()
        return next((str(team["team_id"]) for team in teams if team["team_alias"] == alias), None)

    def team_id(self, alias: str) -> str:
        found = self.find_team(alias)
        if found is None:
            raise LabError(f"team {alias!r} not found; run `llm-bonsai setup teams`")
        return found

    def ensure_team(self, alias: str, models: list[str]) -> str:
        """The team's id, creating the team if it doesn't exist yet."""
        found = self.find_team(alias)
        if found is not None:
            return found
        created = self._http.post("/team/new", json={"team_alias": alias, "models": models}).raise_for_status()
        return str(created.json()["team_id"])

    def key_is_valid(self, key: str) -> bool:
        return self._http.get("/key/info", params={"key": key}).status_code == 200

    def key_spend(self, key: str) -> float:
        """The spend Postgres has recorded for a key so far."""
        info = self._http.get("/key/info", params={"key": key}).raise_for_status().json()
        return float(info["info"]["spend"])

    def generate_key(self, team_id: str, alias: str, **limits: Any) -> str:
        response = self._http.post("/key/generate", json={"team_id": team_id, "key_alias": alias, **limits})
        return str(response.raise_for_status().json()["key"])

    def ensure_user(self, user_id: str, role: str) -> None:
        """Create the user without a key of its own; one that already exists is left alone."""
        if self._http.get("/user/info", params={"user_id": user_id}).status_code == 200:
            return
        body = {"user_id": user_id, "user_role": role, "auto_create_key": False}
        self._http.post("/user/new", json=body).raise_for_status()

    def generate_user_key(self, user_id: str, alias: str, **limits: Any) -> str:
        """A key acting as the user (and with its role), not as a team."""
        response = self._http.post("/key/generate", json={"user_id": user_id, "key_alias": alias, **limits})
        return str(response.raise_for_status().json()["key"])

    def update_key(self, key: str, **limits: Any) -> None:
        self._http.post("/key/update", json={"key": key, **limits}).raise_for_status()

    def delete_key(self, key: str) -> None:
        """Best effort, like `delete_key_alias`: a key that is already gone isn't an error."""
        self._http.post("/key/delete", json={"keys": [key]})

    def delete_key_alias(self, alias: str) -> None:
        self._http.post("/key/delete", json={"key_aliases": [alias]})


class Gateway:
    def __init__(self, tunnels: Tunnels, kube: Kube, env: EnvFile) -> None:
        self._tunnels = tunnels
        self._kube = kube
        self._env = env

    @property
    def base_url(self) -> str:
        return self._tunnels.url(GATEWAY)

    def require(self, wait_s: float = 10, health: str = "liveliness") -> None:
        """Return once the gateway answers its health check, or raise.

        `health="readiness"` also waits for the gateway's database, which the management API needs.
        """
        tunnel = self._tunnels.forward(GATEWAY)
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
                        f"check `kubectl -n {GATEWAY.namespace} get pods`"
                    ) from error
                if not waiting:
                    print(f"  (waiting up to {wait_s:.0f}s for the gateway to answer)", flush=True)
                    waiting = True
                if now - last_reconnect > RECONNECT_AFTER_S:
                    tunnel.restart()
                    last_reconnect = now
                time.sleep(1)

    def reconnect(self) -> None:
        """Move the port-forward to a live pod, e.g. after a redeploy replaced the one it was pinned to."""
        self._tunnels.forward(GATEWAY).restart()

    def client(self, key: str, timeout_s: float | None = None) -> openai.OpenAI:
        """OpenAI SDK pointed at the gateway. No SDK retries: the probes show raw behaviour."""
        return openai.OpenAI(
            base_url=f"{self.base_url}/v1", api_key=key, max_retries=0, timeout=timeout_s or openai.DEFAULT_TIMEOUT
        )

    @contextmanager
    def admin(self) -> Generator[GatewayAdmin, None, None]:
        master_key = self._env.get("LITELLM_MASTER_KEY")
        self.require(health="readiness")
        headers = {"Authorization": f"Bearer {master_key}"}
        with httpx.Client(base_url=self.base_url, headers=headers, timeout=30) as http:
            yield GatewayAdmin(http)

    @contextmanager
    def temporary_key(self, purpose: str, team: str, **limits: Any) -> Generator[str, None, None]:
        """A fresh virtual key for one probe run, deleted afterwards.

        Probes that burn through budgets or rate limits get their own key, so re-running
        them never depends on (or exhausts) the long-lived keys from `setup teams`.
        """
        with self.admin() as admin:
            key = admin.generate_key(admin.team_id(team), f"{purpose}-{uuid.uuid4().hex[:6]}", **limits)
            try:
                yield key
            finally:
                admin.delete_key(key)

    def pods(self, include_terminating: bool = False) -> list[GatewayPod]:
        found = self._kube.kubectl_json("get", "pods", "-l", GATEWAY_POD_LABELS, namespace=GATEWAY.namespace)
        pods = [GatewayPod.from_api(pod) for pod in found["items"]]
        return pods if include_terminating else [pod for pod in pods if not pod.terminating]
