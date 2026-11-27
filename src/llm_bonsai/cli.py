"""The `llm-bonsai` command.

`setup` builds the lab, `probe` pokes it, `ui` opens it in a browser, `teardown` removes it.
"""

import signal
import sys
from types import FrameType
from typing import Annotated

import typer

from llm_bonsai import __version__, tunnels
from llm_bonsai.errors import LabError
from llm_bonsai.probe.cli import app as probe_app
from llm_bonsai.setup import infra
from llm_bonsai.setup.cli import app as setup_app

app = typer.Typer(
    help="A tiny local GenAI platform lab: LiteLLM + Langfuse + Ollama on Kubernetes.",
    no_args_is_help=True,
    # Locals in tracebacks would print API keys and passwords.
    pretty_exceptions_show_locals=False,
)
app.add_typer(setup_app, name="setup")
app.add_typer(probe_app, name="probe")


@app.command("ui")
def ui() -> None:
    """Open Langfuse on localhost:3000 and the LiteLLM UI on localhost:4000 until Ctrl-C."""
    tunnels.hold_open_for_browser()


@app.command("teardown")
def teardown(
    cluster: Annotated[bool, typer.Option("--cluster", help="Delete the whole minikube cluster instead.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Uninstall the lab and all its data (model downloads, traces), keeping the cluster and its operators."""
    if not yes:
        what = "the whole minikube cluster" if cluster else "the lab's releases and all their data"
        typer.confirm(f"Delete {what}?", abort=True)
    if cluster:
        infra.delete_cluster()
    else:
        infra.uninstall()


def show_version(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    pass


def exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    # A plain SIGTERM would skip atexit, leaving the commands' port-forwards running.
    sys.exit(128 + signum)


def main() -> None:
    signal.signal(signal.SIGTERM, exit_on_sigterm)
    try:
        app()
    except LabError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
