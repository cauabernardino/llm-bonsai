"""Create the demo teams and one virtual key per team, and save the keys to .env.

Safe to re-run: teams are matched by alias, and a key already in .env is kept
as long as the gateway still recognises it.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
GATEWAY_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")


@dataclass
class Team:
    alias: str
    env_var: str
    models: list[str]
    rpm_limit: int
    max_budget: float


TEAMS = [
    # Tight limits so the demos can hit them: 3 requests/min and a 5-cent budget.
    Team("payments-ai", "LITELLM_KEY_PAYMENTS_AI", ["chat-small", "chat-broken"], 3, 0.05),
    Team("support-bot", "LITELLM_KEY_SUPPORT_BOT", ["chat-small", "embed"], 600, 10.0),
]


def ensure_team(client: httpx.Client, team: Team) -> str:
    teams = client.get("/team/list").raise_for_status().json()
    for existing in teams:
        if existing["team_alias"] == team.alias:
            return existing["team_id"]
    created = client.post(
        "/team/new", json={"team_alias": team.alias, "models": team.models}
    ).raise_for_status()
    return created.json()["team_id"]


def key_is_valid(client: httpx.Client, key: str) -> bool:
    return client.get("/key/info", params={"key": key}).status_code == 200


def create_key(client: httpx.Client, team: Team, team_id: str) -> str:
    alias = f"{team.alias}-key"
    # A key left over from a lost .env would block the alias; remove it first.
    client.post("/key/delete", json={"key_aliases": [alias]})
    response = client.post(
        "/key/generate",
        json={
            "team_id": team_id,
            "key_alias": alias,
            "models": team.models,
            "rpm_limit": team.rpm_limit,
            "max_budget": team.max_budget,
        },
    ).raise_for_status()
    return response.json()["key"]


def main() -> None:
    load_dotenv(ENV_FILE)
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("LITELLM_MASTER_KEY missing from .env; run `make secrets` first")

    headers = {"Authorization": f"Bearer {master_key}"}
    with httpx.Client(base_url=GATEWAY_URL, headers=headers, timeout=30) as client:
        try:
            client.get("/health/readiness").raise_for_status()
        except httpx.HTTPError as error:
            sys.exit(f"gateway not reachable at {GATEWAY_URL} ({error}); is `make pf` running?")

        for team in TEAMS:
            team_id = ensure_team(client, team)
            key = os.environ.get(team.env_var)
            if key and key_is_valid(client, key):
                print(f"{team.alias}: team {team_id}, key unchanged ({team.env_var})")
                continue
            key = create_key(client, team, team_id)
            set_key(ENV_FILE, team.env_var, key)
            print(
                f"{team.alias}: team {team_id}, new key saved as {team.env_var} "
                f"(models={team.models}, rpm={team.rpm_limit}, budget=${team.max_budget})"
            )


if __name__ == "__main__":
    main()
