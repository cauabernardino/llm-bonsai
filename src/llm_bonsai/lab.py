"""The `Lab`: everything a command talks to, built once per command and closed when it ends."""

from types import TracebackType

import typer

from llm_bonsai.clients.gateway import Gateway
from llm_bonsai.clients.kube import Kube
from llm_bonsai.clients.langfuse import Langfuse
from llm_bonsai.clients.tunnels import Tunnels
from llm_bonsai.config import LabConfig
from llm_bonsai.setup.infra import Installer


class Lab:
    """Building one touches nothing: port-forwards open only when a client first needs them."""

    def __init__(self, config: LabConfig) -> None:
        self.config = config
        self.env = config.env
        self.kube = Kube(config)
        self.tunnels = Tunnels(self.kube)
        self.gateway = Gateway(self.tunnels, self.kube, self.env)
        self.langfuse = Langfuse(self.tunnels, self.env)
        self.installer = Installer(self.kube, self.env)

    @classmethod
    def of(cls, ctx: typer.Context) -> "Lab":
        """The Lab the root command put on the context."""
        lab = ctx.find_object(cls)
        if lab is None:
            raise RuntimeError("no Lab on the context; commands must run under the root `llm-bonsai` app")
        return lab

    def close(self) -> None:
        self.langfuse.close()  # flushes the SDK, so it must go before the port-forward it sends through
        self.tunnels.close()

    def __enter__(self) -> "Lab":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
