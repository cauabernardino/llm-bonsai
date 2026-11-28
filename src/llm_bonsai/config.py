"""Where the lab lives: its home (holding .env), the manifests it installs, and the cluster context."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key

from llm_bonsai.errors import LabError

# A profile of its own, so the lab never touches another minikube cluster on the machine.
DEFAULT_CONTEXT = "llm-bonsai"
PACKAGE = Path(__file__).resolve().parent
# Helm values and manifests ship inside the package. kubectl and helm need real files,
# which pip and uv always provide: they install wheels unzipped.
INFRA = PACKAGE / "infra"
# Only meaningful when running from a source checkout (src layout); in an installed
# wheel this is the environment's lib/ directory.
CHECKOUT = PACKAGE.parents[1]


def lab_home(environ: Mapping[str, str]) -> Path:
    """The directory holding .env: $LLM_BONSAI_HOME, else the checkout when running from
    one (`uv run llm-bonsai`), else ~/.config/llm-bonsai for an installed package."""
    if explicit := environ.get("LLM_BONSAI_HOME"):
        return Path(explicit).expanduser()
    if (CHECKOUT / "pyproject.toml").is_file():
        return CHECKOUT
    config_root = environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_root) / "llm-bonsai"


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
            raise LabError(f"{name} missing from {self.path}; run `llm-bonsai {step}`")
        return value

    def set(self, name: str, value: str) -> None:
        self._create()
        set_key(self.path, name, value)

    def fill_missing(self, generators: Mapping[str, Callable[[], str]]) -> list[str]:
        """Generate the values that are missing and return their names; existing ones are kept."""
        self._create()
        existing = dotenv_values(self.path)
        added = [name for name in generators if not existing.get(name)]
        for name in added:
            set_key(self.path, name, generators[name]())
        return added

    def _create(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600)


@dataclass(frozen=True)
class LabConfig:
    home: Path
    # Every kubectl/helm call is pinned to this context, so a command can never act on
    # whatever other cluster happens to be current. It is also the lab's minikube profile:
    # minikube names a profile's context after the profile.
    kube_context: str

    @classmethod
    def from_environment(cls) -> "LabConfig":
        return cls(home=lab_home(os.environ), kube_context=os.environ.get("LLM_BONSAI_CONTEXT", DEFAULT_CONTEXT))

    @property
    def minikube_profile(self) -> str:
        return self.kube_context

    @property
    def env(self) -> EnvFile:
        return EnvFile(self.home / ".env")


def infra_file(relative: str) -> Path:
    """A Helm values file or manifest shipped with the package, by its path under infra/."""
    path = INFRA / relative
    if not path.is_file():
        raise LabError(f"{relative} is missing from the installed package ({INFRA}); reinstall llm-bonsai")
    return path
