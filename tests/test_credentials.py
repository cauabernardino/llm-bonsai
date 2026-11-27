import base64
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from llm_bonsai.errors import LabError
from llm_bonsai.setup import credentials


def test_fill_env_file_adds_missing_values_and_keeps_existing_ones(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_DB_PASSWORD=keep-me\n")

    added = credentials.fill_env_file(env_file)

    values = dotenv_values(env_file)
    assert "LITELLM_DB_PASSWORD" not in added
    assert values["LITELLM_DB_PASSWORD"] == "keep-me"
    assert set(credentials.GENERATORS) <= set(values)
    assert str(values["LITELLM_MASTER_KEY"]).startswith("sk-")
    assert len(str(values["LANGFUSE_ENCRYPTION_KEY"])) == 64
    assert credentials.fill_env_file(env_file) == []


def test_secrets_come_from_the_env_file_not_the_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    credentials.fill_env_file(env_file)
    monkeypatch.setenv("LANGFUSE_S3_PASSWORD", "from-the-shell")

    by_name = {secret["metadata"]["name"]: secret for secret in credentials.kubernetes_secrets(env_file)}

    assert set(by_name) == {"litellm-master-key", "litellm-db", "litellm-langfuse", "langfuse"}
    langfuse = {k: base64.b64decode(v).decode() for k, v in by_name["langfuse"]["data"].items()}
    identity = json.loads(langfuse["seaweedfs_s3_config"])["identities"][0]
    assert identity["credentials"][0]["secretKey"] == langfuse["s3-password"]
    assert langfuse["s3-password"] == dotenv_values(env_file)["LANGFUSE_S3_PASSWORD"] != "from-the-shell"
    assert by_name["langfuse"]["metadata"]["namespace"] == "observability"


def test_missing_value_is_a_lab_error(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LITELLM_MASTER_KEY=sk-x\n")
    with pytest.raises(LabError, match=r"missing from \.env"):
        credentials.kubernetes_secrets(env_file)
