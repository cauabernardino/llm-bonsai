"""The gateway's management API and the teams setup, against a fake LiteLLM."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from llm_bonsai.catalog import TEAMS
from llm_bonsai.clients.gateway import GatewayAdmin
from llm_bonsai.config import EnvFile
from llm_bonsai.errors import LabError
from llm_bonsai.models import GatewayPod, Observation
from llm_bonsai.setup.prometheus import SCRAPE_KEY_ENV, SCRAPE_LIMITS, ensure_scrape_key
from llm_bonsai.setup.teams import ensure_teams


class FakeLiteLLM:
    """Just enough of LiteLLM's /team, /key and /user endpoints, recording every call."""

    def __init__(self, teams: dict[str, str], valid_keys: tuple[str, ...] = ()) -> None:
        self.teams = dict(teams)  # alias -> id
        self.valid_keys = set(valid_keys)
        self.users: dict[str, str] = {}  # id -> role
        self.calls: list[tuple[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else dict(request.url.params)
        self.calls.append((path, body))
        if path == "/team/list":
            return httpx.Response(200, json=[{"team_alias": a, "team_id": i} for a, i in self.teams.items()])
        if path == "/team/new":
            self.teams[body["team_alias"]] = f"id-{body['team_alias']}"
            return httpx.Response(200, json={"team_id": self.teams[body["team_alias"]]})
        if path == "/key/info":
            if body["key"] not in self.valid_keys:
                return httpx.Response(404)
            return httpx.Response(200, json={"info": {"spend": 0.0123}})
        if path == "/user/info":
            return httpx.Response(200 if body["user_id"] in self.users else 404, json={})
        if path == "/user/new":
            self.users[body["user_id"]] = body["user_role"]
            return httpx.Response(200, json={})
        if path == "/key/generate":
            return httpx.Response(200, json={"key": f"sk-{body['key_alias']}"})
        return httpx.Response(200, json={})

    def admin(self) -> GatewayAdmin:
        return GatewayAdmin(httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(self.handle)))

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def test_team_id_of_a_missing_team_points_at_setup_teams() -> None:
    with pytest.raises(LabError, match="setup teams"):
        FakeLiteLLM({}).admin().team_id("payments-ai")


def test_key_spend_reads_key_info() -> None:
    assert FakeLiteLLM({}, valid_keys=("sk-1",)).admin().key_spend("sk-1") == 0.0123


def test_ensure_teams_creates_teams_and_saves_new_keys(tmp_path: Path) -> None:
    fake, env = FakeLiteLLM({}), EnvFile(tmp_path / ".env")

    ensure_teams(fake.admin(), env)

    assert set(fake.teams) == {team.alias for team in TEAMS}
    for team in TEAMS:
        assert env.get(team.env_var) == f"sk-{team.alias}-key"
    assert fake.paths().count("/key/delete") == len(TEAMS)  # stale aliases cleared first


def test_ensure_teams_keeps_a_valid_key_and_updates_its_limits(tmp_path: Path) -> None:
    team = TEAMS[0]
    env = EnvFile(tmp_path / ".env")
    env.set(team.env_var, "sk-existing")
    fake = FakeLiteLLM({t.alias: f"id-{t.alias}" for t in TEAMS}, valid_keys=("sk-existing",))

    ensure_teams(fake.admin(), env)

    assert env.get(team.env_var) == "sk-existing"
    assert ("/key/update", {"key": "sk-existing", **team.key_limits()}) in fake.calls
    assert "/team/new" not in fake.paths()


def test_scrape_key_belongs_to_a_read_only_admin_and_only_reaches_metrics(tmp_path: Path) -> None:
    fake, env = FakeLiteLLM({}), EnvFile(tmp_path / ".env")

    key = ensure_scrape_key(fake.admin(), env)

    assert fake.users == {"prometheus": "proxy_admin_viewer"}
    generated = next(body for path, body in fake.calls if path == "/key/generate")
    assert generated == {"user_id": "prometheus", "key_alias": "prometheus-scrape", **SCRAPE_LIMITS}
    assert env.get(SCRAPE_KEY_ENV) == key == "sk-prometheus-scrape"


def test_scrape_key_is_kept_while_valid(tmp_path: Path) -> None:
    env = EnvFile(tmp_path / ".env")
    env.set(SCRAPE_KEY_ENV, "sk-existing")
    fake = FakeLiteLLM({}, valid_keys=("sk-existing",))

    assert ensure_scrape_key(fake.admin(), env) == "sk-existing"
    assert ("/key/update", {"key": "sk-existing", **SCRAPE_LIMITS}) in fake.calls
    assert "/key/generate" not in fake.paths()


def test_ensure_user_leaves_an_existing_user_alone() -> None:
    fake = FakeLiteLLM({})
    fake.users["prometheus"] = "proxy_admin_viewer"

    fake.admin().ensure_user("prometheus", role="proxy_admin_viewer")

    assert "/user/new" not in fake.paths()


def test_gateway_pod_from_the_kubernetes_api() -> None:
    pod = GatewayPod.from_api(
        {
            "metadata": {"name": "litellm-abc", "deletionTimestamp": "2026-11-28T10:00:00Z"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
    )
    assert pod == GatewayPod("litellm-abc", ready=True, terminating=True)
    assert GatewayPod.from_api({"metadata": {"name": "new"}}) == GatewayPod("new", ready=False, terminating=False)


def test_observation_from_the_langfuse_api() -> None:
    generation = Observation.from_api(
        {
            "id": "2b9c19f7ea8ae5d0",
            "traceId": "7f77cd706fc8037ddc7022d3575c95da",
            "parentObservationId": None,
            "type": "GENERATION",
            "name": "litellm_request",
            "promptName": "",
            "promptVersion": None,
            "startTime": "2026-11-27T23:04:01.062Z",
            "level": "DEFAULT",
            "latency": 0.825,
            "model": "chat-small",
            "statusMessage": "",
            "totalUsage": 413,
            "totalCost": 0.0426,
            "metadata": {"attributes.langfuse.trace.tags": ["user_api_key_team_alias:payments-ai"]},
        }
    )
    assert (generation.total_tokens, generation.total_cost, generation.latency) == (413, 0.0426, 0.825)
    assert generation.tags == ["user_api_key_team_alias:payments-ai"]
    assert generation.start_time.tzinfo is not None
    assert generation.parent_id is None and generation.prompt_name is None
