"""Dev-only secrets: random values in .env, mirrored into Kubernetes Secrets."""

import base64
import json
import secrets
from collections.abc import Callable
from typing import Any

from llm_bonsai.catalog import LLM_NAMESPACE, OBSERVABILITY_NAMESPACE
from llm_bonsai.config import EnvFile


def password() -> str:
    return secrets.token_urlsafe(24)


GENERATORS: dict[str, Callable[[], str]] = {
    # LiteLLM requires the master key to start with "sk-".
    "LITELLM_MASTER_KEY": lambda: f"sk-{password()}",
    "LITELLM_DB_PASSWORD": password,
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
    "LANGFUSE_INIT_USER_EMAIL": lambda: "admin@example.com",
    "LANGFUSE_INIT_USER_PASSWORD": password,
}


# The Secrets holding passwords that Postgres and ClickHouse keep from their first start:
# replacing them breaks the lab until its volumes are wiped.
PERSISTED_SECRETS = (("litellm-db", LLM_NAMESPACE), ("langfuse", OBSERVABILITY_NAMESPACE))


def secret(name: str, namespace: str, data: dict[str, str]) -> dict[str, Any]:
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": encoded,
    }


def kubernetes_secrets(env: EnvFile) -> list[dict[str, Any]]:
    """The lab's Secrets, built from .env alone (never from shell variables)."""
    db_password = env.get("LANGFUSE_DB_PASSWORD")
    redis_password = env.get("LANGFUSE_REDIS_PASSWORD")
    s3_password = env.get("LANGFUSE_S3_PASSWORD")
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
        secret("litellm-master-key", LLM_NAMESPACE, {"master-key": env.get("LITELLM_MASTER_KEY")}),
        secret("litellm-db", LLM_NAMESPACE, {"username": "litellm", "password": env.get("LITELLM_DB_PASSWORD")}),
        secret(
            "litellm-langfuse",
            LLM_NAMESPACE,
            {
                "LANGFUSE_PUBLIC_KEY": env.get("LANGFUSE_PUBLIC_KEY"),
                "LANGFUSE_SECRET_KEY": env.get("LANGFUSE_SECRET_KEY"),
            },
        ),
        # One Secret for the whole Langfuse chart: its subcharts each read their own keys.
        secret(
            "langfuse",
            OBSERVABILITY_NAMESPACE,
            {
                "salt": env.get("LANGFUSE_SALT"),
                "encryption-key": env.get("LANGFUSE_ENCRYPTION_KEY"),
                "nextauth-secret": env.get("LANGFUSE_NEXTAUTH_SECRET"),
                "postgresql-password": db_password,
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": db_password,
                "POSTGRES_DB": "langfuse",
                "USERDB_USER": "langfuse",
                "USERDB_PASSWORD": db_password,
                "clickhouse-password": env.get("LANGFUSE_CLICKHOUSE_PASSWORD"),
                "redis-password": redis_password,
                "default": redis_password,
                "s3-user": "langfuse",
                "s3-password": s3_password,
                "seaweedfs_s3_config": json.dumps(seaweedfs_identities),
                "init-public-key": env.get("LANGFUSE_PUBLIC_KEY"),
                "init-secret-key": env.get("LANGFUSE_SECRET_KEY"),
                "init-user-email": env.get("LANGFUSE_INIT_USER_EMAIL"),
                "init-user-password": env.get("LANGFUSE_INIT_USER_PASSWORD"),
            },
        ),
    ]
