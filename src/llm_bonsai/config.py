"""Where the lab lives: the repository, its .env and the cluster context.

The package runs from its own checkout (`uv run llm-bonsai`), since the Helm values
and manifests under infra/ are part of the repository, not of the package.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key

from llm_bonsai.errors import LabError

CHECKOUT = Path(__file__).resolve().parents[2]


class EnvFile:
    """The lab's .env, its only source of credentials.

    Shell variables are ignored on purpose: an exported LANGFUSE_SECRET_KEY for some
    other Langfuse must not leak into the lab. The file is read on every call because
    `setup secrets` and `setup teams` write to it while the process runs.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def find(self, name: str) -> str | None:
        return dotenv_values(self.path).get(name) or None

    def get(self, name: str) -> str:
        value = self.find(name)
        if value is None:
            step = "setup teams" if name.startswith("LITELLM_KEY_") else "setup secrets"
            raise LabError(f"{name} missing from {self.path.name}; run `llm-bonsai {step}`")
        return value

    def set(self, name: str, value: str) -> None:
        self.path.touch(mode=0o600)
        set_key(self.path, name, value)

    def fill_missing(self, generators: Mapping[str, Callable[[], str]]) -> list[str]:
        """Generate the values that are missing and return their names; existing ones are kept."""
        self.path.touch(mode=0o600)
        existing = dotenv_values(self.path)
        added = [name for name in generators if not existing.get(name)]
        for name in added:
            set_key(self.path, name, generators[name]())
        return added


@dataclass(frozen=True)
class LabConfig:
    root: Path
    # Every kubectl/helm call is pinned to this context, so a command can never act on
    # whatever other cluster happens to be current.
    kube_context: str

    @classmethod
    def from_environment(cls) -> "LabConfig":
        return cls(root=CHECKOUT, kube_context=os.environ.get("LLM_BONSAI_CONTEXT", "minikube"))

    @property
    def env(self) -> EnvFile:
        return EnvFile(self.root / ".env")

    def infra_file(self, relative: str) -> Path:
        path = self.root / "infra" / relative
        if not path.is_file():
            raise LabError(f"{path} not found; run the CLI from the llm-bonsai checkout (`uv run llm-bonsai ...`)")
        return path
