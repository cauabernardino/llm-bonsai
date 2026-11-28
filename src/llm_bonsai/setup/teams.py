"""The demo teams (catalog.TEAMS), each with one long-lived virtual key saved to .env.

Safe to re-run: teams are matched by alias, and a key already in .env is kept as long
as the gateway still recognises it, with its limits updated to match TEAMS.
"""

from llm_bonsai import report
from llm_bonsai.catalog import TEAMS
from llm_bonsai.clients.gateway import GatewayAdmin
from llm_bonsai.config import EnvFile


def ensure_teams(admin: GatewayAdmin, env: EnvFile) -> None:
    for team in TEAMS:
        team_id = admin.ensure_team(team.alias, team.models)
        key = env.find(team.env_var)
        limits = f"models {', '.join(team.models)}; {team.rpm_limit} requests/min; ${team.max_budget:g} budget"
        if key and admin.key_is_valid(key):
            # Changing a limit in TEAMS takes effect on re-run.
            admin.update_key(key, **team.key_limits())
            report.line((team.alias, "bold"), f": team {team_id}, existing key updated ({limits})")
            continue
        alias = f"{team.alias}-key"
        # A key left over from a lost .env would block the alias; remove it first.
        admin.delete_key_alias(alias)
        env.set(team.env_var, admin.generate_key(team_id, alias, **team.key_limits()))
        report.line((team.alias, "bold"), f": team {team_id}, new key saved as {team.env_var} ({limits})")
