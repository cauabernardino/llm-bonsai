"""The demo teams, each with one long-lived virtual key saved to .env.

Safe to re-run: teams are matched by alias, and a key already in .env is kept as long
as the gateway still recognises it, with its limits updated to match TEAMS.
"""

from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import dotenv_values, set_key

from llm_bonsai import gateway
from llm_bonsai.config import ENV_FILE


@dataclass(frozen=True)
class Team:
    alias: str
    env_var: str
    models: list[str]
    rpm_limit: int
    max_budget: float

    def key_limits(self) -> dict[str, Any]:
        return {"models": self.models, "rpm_limit": self.rpm_limit, "max_budget": self.max_budget}


# Budgets are generous on purpose: the probes that need to run out of budget create
# their own throwaway keys, and load/chaos runs spend a few fake dollars each.
TEAMS = [
    Team("payments-ai", "LITELLM_KEY_PAYMENTS_AI", ["chat-small", "chat-broken"], rpm_limit=3, max_budget=1000.0),
    Team("support-bot", "LITELLM_KEY_SUPPORT_BOT", ["chat-small", "embed"], rpm_limit=600, max_budget=1000.0),
]


def ensure_team(api: httpx.Client, team: Team) -> str:
    teams: list[dict[str, Any]] = api.get("/team/list").raise_for_status().json()
    for existing in teams:
        if existing["team_alias"] == team.alias:
            return str(existing["team_id"])
    created = api.post("/team/new", json={"team_alias": team.alias, "models": team.models}).raise_for_status()
    return str(created.json()["team_id"])


def key_is_valid(api: httpx.Client, key: str) -> bool:
    return api.get("/key/info", params={"key": key}).status_code == 200


def create_key(api: httpx.Client, team: Team, team_id: str) -> str:
    alias = f"{team.alias}-key"
    # A key left over from a lost .env would block the alias; remove it first.
    api.post("/key/delete", json={"key_aliases": [alias]})
    response = api.post(
        "/key/generate", json={"team_id": team_id, "key_alias": alias, **team.key_limits()}
    ).raise_for_status()
    return str(response.json()["key"])


def ensure_teams() -> None:
    """Create the demo teams and one virtual key each; saves the keys to .env. Safe to re-run."""
    saved = dotenv_values(ENV_FILE)
    with gateway.admin() as api:
        for team in TEAMS:
            team_id = ensure_team(api, team)
            key = saved.get(team.env_var)
            limits = f"models {', '.join(team.models)}; {team.rpm_limit} requests/min; ${team.max_budget:g} budget"
            if key and key_is_valid(api, key):
                # Changing a limit in TEAMS takes effect on re-run.
                api.post("/key/update", json={"key": key, **team.key_limits()}).raise_for_status()
                print(f"{team.alias}: team {team_id}, existing key updated ({limits})")
                continue
            key = create_key(api, team, team_id)
            set_key(ENV_FILE, team.env_var, key)
            print(f"{team.alias}: team {team_id}, new key saved as {team.env_var} ({limits})")
