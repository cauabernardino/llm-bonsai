"""The `llm-bonsai` command.

`setup` builds the lab, `probe` pokes it, `ui` opens it in a browser, `teardown` removes it.
"""

import signal
import sys
import threading
from types import FrameType
from typing import Annotated

import typer

from llm_bonsai import __version__
from llm_bonsai.catalog import GATEWAY, LANGFUSE
from llm_bonsai.clients.tunnels import PortForward, port_in_use
from llm_bonsai.config import LabConfig
from llm_bonsai.errors import LabError
from llm_bonsai.lab import Lab
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
def ui(ctx: typer.Context) -> None:
    """Open Langfuse on localhost:3000 and the LiteLLM UI on localhost:4000 until Ctrl-C."""
    # Fixed ports, unlike the commands' own tunnels: Langfuse's login only works on the
    # URL it was configured with (NEXTAUTH_URL=http://localhost:3000).
    pages = [(LANGFUSE, 3000), (GATEWAY, 4000)]
    busy = [port for _, port in pages if port_in_use(port)]
    if busy:
        raise LabError(f"port {' and '.join(map(str, busy))} already in use (an old `kubectl port-forward`?)")
    kube = Lab.of(ctx).kube
    forwards = [PortForward(kube, service, port) for service, port in pages]
    try:
        for forward in forwards:
            forward.open()
        print("Langfuse:   http://localhost:3000     log in with LANGFUSE_INIT_USER_EMAIL / _PASSWORD from .env")
        print("LiteLLM UI: http://localhost:4000/ui  log in as admin with LITELLM_MASTER_KEY from .env")
        print("Ctrl-C closes both.", flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nclosed")
    finally:
        for forward in forwards:
            forward.close()


@app.command("teardown")
def teardown(
    ctx: typer.Context,
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
        Lab.of(ctx).installer.uninstall()


def show_version(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    # Closed when the command finishes, fails or is interrupted: that is what closes its port-forwards.
    ctx.obj = ctx.with_resource(Lab(LabConfig.from_environment()))


def exit_on_sigterm(signum: int, frame: FrameType | None) -> None:
    # A plain SIGTERM would kill the process without unwinding, leaving the port-forwards running.
    # Ignore any further ones (`uv run` forwards the signal it got too) so they can't interrupt the cleanup.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.exit(128 + signum)


def main() -> None:
    signal.signal(signal.SIGTERM, exit_on_sigterm)
    try:
        app()
    except LabError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
