from typing import Annotated

import typer

from llm_bonsai.setup import infra, preflight, teams

app = typer.Typer(
    help="Build the lab, one step at a time or all at once. Every step is safe to re-run.",
    no_args_is_help=True,
)

app.command("preflight")(preflight.run)
app.command("cluster")(infra.create_cluster)
app.command("namespaces")(infra.install_namespaces)
app.command("secrets")(infra.install_secrets)
app.command("ollama")(infra.install_ollama)
app.command("postgres")(infra.install_postgres)
app.command("valkey")(infra.install_valkey)
app.command("cert-manager")(infra.install_cert_manager)
app.command("clickhouse-operator")(infra.install_clickhouse_operator)
app.command("langfuse")(infra.install_langfuse)


@app.command("litellm")
def litellm(
    set_values: Annotated[
        list[str] | None,
        typer.Option("--set", metavar="KEY=VALUE", help="One-off Helm override, e.g. gateway.replicaCount=2."),
    ] = None,
) -> None:
    """Install the LiteLLM gateway (needs Postgres and Valkey). Re-run without --set to undo overrides."""
    infra.install_litellm(set_values or [])


app.command("all")(infra.install_all)
app.command("teams")(teams.ensure_teams)
