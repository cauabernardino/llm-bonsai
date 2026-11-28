from typing import Annotated

import typer

from llm_bonsai import report
from llm_bonsai.lab import Lab
from llm_bonsai.setup import infra, preflight, prometheus, teams

app = typer.Typer(
    help="Build the lab, one step at a time or all at once. Every step is safe to re-run.",
    no_args_is_help=True,
)


@app.command("preflight")
def preflight_command(ctx: typer.Context) -> None:
    """Check the host against the hard requirements (OS, Docker memory and CPUs, disk, tool versions). Read-only."""
    if not preflight.run(Lab.of(ctx).config.kube_context):
        raise typer.Exit(code=1)


@app.command("cluster")
def cluster(ctx: typer.Context) -> None:
    """Start the lab's own minikube profile (6 CPUs, 14 GiB, 40 GB of disk), plus metrics-server."""
    infra.create_cluster(Lab.of(ctx).config.minikube_profile)


@app.command("namespaces")
def namespaces(ctx: typer.Context) -> None:
    """Create the llm and observability namespaces."""
    Lab.of(ctx).installer.install_namespaces()


@app.command("secrets")
def secrets(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option("--force", help="Generate missing values even if the lab already has its Secrets.")
    ] = False,
) -> None:
    """Fill .env with random dev secrets (keeping existing ones) and apply them as Kubernetes Secrets."""
    Lab.of(ctx).installer.install_secrets(force)


@app.command("ollama")
def ollama(ctx: typer.Context) -> None:
    """Install Ollama and pull qwen2.5:1.5b and nomic-embed-text (first run: a few minutes)."""
    Lab.of(ctx).installer.install_ollama()


@app.command("postgres")
def postgres(ctx: typer.Context) -> None:
    """Install the gateway's Postgres (keys, teams, spend)."""
    Lab.of(ctx).installer.install_postgres()


@app.command("valkey")
def valkey(ctx: typer.Context) -> None:
    """Install Valkey, the gateway's shared cache and rate-limit store."""
    Lab.of(ctx).installer.install_valkey()


@app.command("cert-manager")
def cert_manager(ctx: typer.Context) -> None:
    """Install cert-manager, a prerequisite of the ClickHouse operator."""
    Lab.of(ctx).installer.install_cert_manager()


@app.command("clickhouse-operator")
def clickhouse_operator(ctx: typer.Context) -> None:
    """Install the ClickHouse operator, a prerequisite of the Langfuse chart."""
    Lab.of(ctx).installer.install_clickhouse_operator()


@app.command("langfuse")
def langfuse(ctx: typer.Context) -> None:
    """Install Langfuse v4 with ClickHouse, Postgres, Valkey and SeaweedFS (first run: up to 15 min)."""
    Lab.of(ctx).installer.install_langfuse()


@app.command("litellm")
def litellm(
    ctx: typer.Context,
    set_values: Annotated[
        list[str] | None,
        typer.Option("--set", metavar="KEY=VALUE", help="One-off Helm override, e.g. gateway.replicaCount=2."),
    ] = None,
) -> None:
    """Install the LiteLLM gateway (needs Postgres and Valkey). Re-run without --set to undo overrides."""
    Lab.of(ctx).installer.install_litellm(set_values or [])


@app.command("all")
def all_steps(ctx: typer.Context) -> None:
    """Run every install step, namespaces through litellm, in order (not `cluster` or `teams`)."""
    Lab.of(ctx).installer.install_all()


@app.command("teams")
def teams_command(ctx: typer.Context) -> None:
    """Create the demo teams and one virtual key each; saves the keys to .env. Safe to re-run."""
    lab = Lab.of(ctx)
    with lab.gateway.admin() as admin:
        teams.ensure_teams(admin, lab.env)


@app.command("prometheus")
def prometheus_command(ctx: typer.Context) -> None:
    """Optional: one Prometheus pod scraping the gateway with a key limited to /metrics (needs the gateway)."""
    lab = Lab.of(ctx)
    with lab.gateway.admin() as admin:
        scrape_key = prometheus.ensure_scrape_key(admin, lab.env)
    lab.installer.install_prometheus(scrape_key)
    report.line(("✓ Prometheus is up; ", "bold green"), "`llm-bonsai ui` serves it on http://localhost:9090")
