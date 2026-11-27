"""Dev-only secrets: random values in .env, mirrored into Kubernetes Secrets."""

import base64
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

from llm_bonsai import kube
from llm_bonsai.config import ENV_FILE
from llm_bonsai.errors import LabError


def password() -> str:
    return secrets.token_urlsafe(24)


GENERATORS: dict[str, Callable[[], str]] = {
    # LiteLLM requires the master key to start with "sk-".
    "LITELLM_MASTER_KEY": lambda: f"sk-{password()}",
    "LITELLM_DB_PASSWORD": password,
    # Langfuse internals.
    "LANGFUSE_SALT": password,
    "LANGFUSE_ENCRYPTION_KEY": lambda: secrets.token_hex(32),  # must be 64 hex chars
    "LANGFUSE_NEXTAUTH_SECRET": password,
    "LANGFUSE_DB_PASSWORD": password,
    "LANGFUSE_CLICKHOUSE_PASSWORD": password,
    "LANGFUSE_REDIS_PASSWORD": password,
    "LANGFUSE_S3_PASSWORD": password,
    # Project API keys, created by Langfuse's headless init. These names are also what
    # the Langfuse SDK and LiteLLM's langfuse_otel callback read.
    "LANGFUSE_PUBLIC_KEY": lambda: f"pk-lf-{secrets.token_hex(16)}",
    "LANGFUSE_SECRET_KEY": lambda: f"sk-lf-{secrets.token_hex(16)}",
    # UI login.
    "LANGFUSE_INIT_USER_EMAIL": lambda: "admin@example.com",
    "LANGFUSE_INIT_USER_PASSWORD": password,
}


def fill_env_file(path: Path = ENV_FILE) -> list[str]:
    """Add the missing secrets to .env and return their names.

    Existing values are kept: rotating a database password after the database was
    initialised would lock the app out.
    """
    path.touch(mode=0o600)
    existing = dotenv_values(path)
    added = [name for name in GENERATORS if not existing.get(name)]
    for name in added:
        set_key(path, name, GENERATORS[name]())
    return added


def secret(name: str, namespace: str, data: dict[str, str]) -> dict[str, Any]:
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": encoded,
    }


def kubernetes_secrets(path: Path = ENV_FILE) -> list[dict[str, Any]]:
    """The lab's Secrets, built from .env alone (never from shell variables)."""
    values = dotenv_values(path)

    def env(name: str) -> str:
        value = values.get(name)
        if not value:
            raise LabError(f"{name} missing from {path.name}")
        return value

    db_password = env("LANGFUSE_DB_PASSWORD")
    redis_password = env("LANGFUSE_REDIS_PASSWORD")
    s3_password = env("LANGFUSE_S3_PASSWORD")
    # SeaweedFS reads its S3 identities from this JSON; Langfuse signs requests with the same pair.
    seaweedfs_identities = {
        "identities": [
            {
                "name": "langfuse",
                "credentials": [{"accessKey": "langfuse", "secretKey": s3_password}],
                "actions": ["Admin", "Read", "Write", "List", "Tagging"],
            }
        ]
    }
    return [
        secret("litellm-master-key", "llm", {"master-key": env("LITELLM_MASTER_KEY")}),
        secret("litellm-db", "llm", {"username": "litellm", "password": env("LITELLM_DB_PASSWORD")}),
        secret(
            "litellm-langfuse",
            "llm",
            {
                "LANGFUSE_PUBLIC_KEY": env("LANGFUSE_PUBLIC_KEY"),
                "LANGFUSE_SECRET_KEY": env("LANGFUSE_SECRET_KEY"),
            },
        ),
        # One Secret for the whole Langfuse chart: its subcharts each read their own keys.
        secret(
            "langfuse",
            "observability",
            {
                "salt": env("LANGFUSE_SALT"),
                "encryption-key": env("LANGFUSE_ENCRYPTION_KEY"),
                "nextauth-secret": env("LANGFUSE_NEXTAUTH_SECRET"),
                "postgresql-password": db_password,
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": db_password,
                "POSTGRES_DB": "langfuse",
                "USERDB_USER": "langfuse",
                "USERDB_PASSWORD": db_password,
                "clickhouse-password": env("LANGFUSE_CLICKHOUSE_PASSWORD"),
                "redis-password": redis_password,
                "default": redis_password,
                "s3-user": "langfuse",
                "s3-password": s3_password,
                "seaweedfs_s3_config": json.dumps(seaweedfs_identities),
                "init-public-key": env("LANGFUSE_PUBLIC_KEY"),
                "init-secret-key": env("LANGFUSE_SECRET_KEY"),
                "init-user-email": env("LANGFUSE_INIT_USER_EMAIL"),
                "init-user-password": env("LANGFUSE_INIT_USER_PASSWORD"),
            },
        ),
    ]


def apply() -> None:
    added = fill_env_file()
    print(f"{ENV_FILE.name}: added {', '.join(added) if added else 'nothing (all present)'}")
    kube.apply_objects(kubernetes_secrets())
