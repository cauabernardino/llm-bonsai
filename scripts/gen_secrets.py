"""Fill .env with random dev-only secrets.

Idempotent: existing values are kept. Rotating a database password after the
database was initialised would lock the app out, so only missing keys are added.
"""

import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def password() -> str:
    return secrets.token_urlsafe(24)


GENERATORS = {
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


def main() -> None:
    ENV_FILE.touch(mode=0o600)
    existing = dotenv_values(ENV_FILE)
    added = [name for name in GENERATORS if not existing.get(name)]
    for name in added:
        set_key(ENV_FILE, name, GENERATORS[name]())
    print(f"{ENV_FILE.name}: added {', '.join(added) if added else 'nothing (all present)'}")


if __name__ == "__main__":
    main()
