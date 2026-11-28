"""The `llm-bonsai` command.

`setup` builds the lab, `probe` pokes it, `ui` opens it in a browser, `teardown` removes it.
"""

import signal
import sys
import threading
from dataclasses import dataclass
from types import FrameType
from typing import Annotated

import typer
from rich.text import Text

from llm_bonsai import __version__, chat, report
from llm_bonsai.catalog import CHAT_SMALL, GATEWAY, LANGFUSE, PROMETHEUS
from llm_bonsai.clients.tunnels import PortForward, port_in_use
from llm_bonsai.config import LabConfig
from llm_bonsai.errors import LabError
from llm_bonsai.lab import Lab
from llm_bonsai.models import Service
from llm_bonsai.probe.cli import app as probe_app
from llm_bonsai.setup import infra
from llm_bonsai.setup.cli import app as setup_app

app = typer.Typer(
    help="A real GenAI platform, deliberately kept tiny: LiteLLM, Ollama, Langfuse and Prometheus on Kubernetes.",
    no_args_is_help=True,
    # Locals in tracebacks would print API keys and passwords.
    pretty_exceptions_show_locals=False,
)
app.add_typer(setup_app, name="setup")
app.add_typer(probe_app, name="probe")


@dataclass(frozen=True)
class Page:
    service: Service
    port: int
    name: str
    path: str
    login: str

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}{self.path}"


# Fixed ports, unlike the commands' own tunnels: Langfuse's login only works on the
# URL it was configured with (NEXTAUTH_URL=http://localhost:3000).
PAGES = [
    Page(LANGFUSE, 3000, "Langfuse", "", "LANGFUSE_INIT_USER_EMAIL / _PASSWORD from .env"),
    Page(GATEWAY, 4000, "LiteLLM UI", "/ui", "user admin, password LITELLM_MASTER_KEY from .env"),
]
PROMETHEUS_PAGE = Page(PROMETHEUS, 9090, "Prometheus", "", "no login; Status → Targets lists the gateway pods")


@app.command("ui")
def ui(ctx: typer.Context) -> None:
    """Open Langfuse on localhost:3000, the LiteLLM UI on localhost:4000 and, if installed, Prometheus on
    localhost:9090, until Ctrl-C."""
    kube = Lab.of(ctx).kube
    pages = list(PAGES)
    if kube.kubectl(
        "get", "service", PROMETHEUS.name, "--ignore-not-found", "-o", "name", namespace=PROMETHEUS.namespace
    ):
        pages.append(PROMETHEUS_PAGE)
    busy = [page.port for page in pages if port_in_use(page.port)]
    if busy:
        raise LabError(f"port {' and '.join(map(str, busy))} already in use (an old `kubectl port-forward`?)")
    forwards = [PortForward(kube, page.service, page.port) for page in pages]
    try:
        for forward in forwards:
            forward.open()
        open_pages = report.table("", "URL", "log in", title="Forwarded to this machine")
        for page in pages:
            open_pages.add_row(Text(page.name, style="bold"), page.url, page.login)
        report.console.print(open_pages)
        report.note("Ctrl-C closes them.")
        threading.Event().wait()
    except KeyboardInterrupt:
        report.note("\nclosed")
    finally:
        for forward in forwards:
            forward.close()


@app.command("chat")
def chat_command(
    ctx: typer.Context,
    model: Annotated[str, typer.Option(help="Gateway model group to talk to.")] = CHAT_SMALL,
    system: Annotated[str | None, typer.Option(help="A system prompt for the whole conversation.")] = None,
) -> None:
    """Chat with a model through the gateway, streamed; the conversation is one Langfuse session."""
    chat.run(Lab.of(ctx), model, system)


@app.command("teardown")
def teardown(
    ctx: typer.Context,
    cluster: Annotated[
        bool, typer.Option("--cluster", help="Delete the lab's whole minikube profile instead.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Uninstall the lab and all its data (model downloads, traces), keeping the cluster and its operators."""
    profile = Lab.of(ctx).config.minikube_profile
    if not yes:
        what = f"the whole minikube profile {profile!r}" if cluster else "the lab's releases and all their data"
        typer.confirm(f"Delete {what}?", abort=True)
    if cluster:
        infra.delete_cluster(profile)
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
        report.error(str(error))
        sys.exit(1)
