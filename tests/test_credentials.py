import base64
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from llm_bonsai.config import EnvFile
from llm_bonsai.errors import LabError
from llm_bonsai.setup import credentials


def test_fill_missing_adds_missing_values_and_keeps_existing_ones(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_DB_PASSWORD=keep-me\n")

    added = EnvFile(env_file).fill_missing(credentials.GENERATORS)

    values = dotenv_values(env_file)
    assert "LITELLM_DB_PASSWORD" not in added
    assert values["LITELLM_DB_PASSWORD"] == "keep-me"
    assert set(credentials.GENERATORS) <= set(values)
    assert str(values["LITELLM_MASTER_KEY"]).startswith("sk-")
    assert len(str(values["LANGFUSE_ENCRYPTION_KEY"])) == 64
    assert EnvFile(env_file).fill_missing(credentials.GENERATORS) == []


def test_secrets_come_from_the_env_file_not_the_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    EnvFile(env_file).fill_missing(credentials.GENERATORS)
    monkeypatch.setenv("LANGFUSE_S3_PASSWORD", "from-the-shell")

    by_name = {secret["metadata"]["name"]: secret for secret in credentials.kubernetes_secrets(EnvFile(env_file))}

    assert set(by_name) == {"litellm-master-key", "litellm-db", "litellm-langfuse", "langfuse"}
    langfuse = {k: base64.b64decode(v).decode() for k, v in by_name["langfuse"]["data"].items()}
    identity = json.loads(langfuse["seaweedfs_s3_config"])["identities"][0]
    assert identity["credentials"][0]["secretKey"] == langfuse["s3-password"]
    assert langfuse["s3-password"] == dotenv_values(env_file)["LANGFUSE_S3_PASSWORD"] != "from-the-shell"
    assert by_name["langfuse"]["metadata"]["namespace"] == "observability"


def test_missing_value_is_a_lab_error(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_MASTER_KEY=sk-x\n")
    with pytest.raises(LabError, match=r"missing from \.env; run `llm-bonsai setup secrets`"):
        credentials.kubernetes_secrets(EnvFile(env_file))


def test_missing_team_key_points_at_setup_teams(tmp_path: Path) -> None:
    with pytest.raises(LabError, match="setup teams"):
        EnvFile(tmp_path / ".env").get("LITELLM_KEY_SUPPORT_BOT")
