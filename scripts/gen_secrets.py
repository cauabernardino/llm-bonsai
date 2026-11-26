"""Fill .env with random dev-only secrets.

Idempotent: existing values are kept. Rotating a database password after the
database was initialised would lock the app out, so only missing keys are added.
"""

import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

GENERATORS = {
    # LiteLLM requires the master key to start with "sk-".
    "LITELLM_MASTER_KEY": lambda: f"sk-{secrets.token_urlsafe(24)}",
    "LITELLM_DB_PASSWORD": lambda: secrets.token_urlsafe(24),
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
