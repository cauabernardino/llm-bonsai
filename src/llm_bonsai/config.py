"""Where the lab lives: the repository, its .env and the cluster context.

The package runs from its own checkout (`uv run llm-bonsai`), since the Helm values
and manifests under infra/ are part of the repository, not of the package.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

from llm_bonsai.errors import LabError

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
ENV_FILE = ROOT / ".env"

# Every kubectl/helm call is pinned to this context, so a command can never act on
# whatever other cluster happens to be current.
KUBE_CONTEXT = os.environ.get("LLM_BONSAI_CONTEXT", "minikube")


def infra_file(relative: str) -> Path:
    path = INFRA / relative
    if not path.is_file():
        raise LabError(f"{path} not found; run the CLI from the llm-bonsai checkout (`uv run llm-bonsai ...`)")
    return path


def env(name: str) -> str:
    """A value from .env, the lab's only source of credentials.

    Shell variables are ignored on purpose: an exported LANGFUSE_SECRET_KEY for some
    other Langfuse must not leak into the lab. The file is read on every call because
    `setup secrets` and `setup teams` write to it while the process runs.
    """
    value = dotenv_values(ENV_FILE).get(name)
    if not value:
        step = "setup teams" if name.startswith("LITELLM_KEY_") else "setup secrets"
        raise LabError(f"{name} missing from .env; run `llm-bonsai {step}`")
    return value
