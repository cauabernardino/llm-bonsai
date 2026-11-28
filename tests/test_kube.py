from pathlib import Path
from typing import Any

import pytest

from llm_bonsai.clients.kube import Kube
from llm_bonsai.config import LabConfig
from llm_bonsai.errors import LabError
from llm_bonsai.models import Chart
from llm_bonsai.setup import infra

CONFIG = LabConfig.from_environment()
KUBE = Kube(LabConfig(root=CONFIG.root, kube_context="lab-ctx"))


def test_upgrade_args_for_a_repo_chart_with_values_and_overrides() -> None:
    chart = Chart(
        release="ollama",
        chart="ollama",
        version="1.0.0",
        namespace="llm",
        repo="https://example.test/",
        values="ollama/values.yaml",
        wait_timeout="10m",
    )
    assert KUBE.upgrade_args(chart, ["a=1", "b="]) == [
        "upgrade", "--install", "ollama", "ollama", "--version", "1.0.0", "-n", "llm",
        "--repo", "https://example.test/", "-f", str(CONFIG.root / "infra/ollama/values.yaml"),
        "--wait", "--timeout", "10m", "--set", "a=1", "--set", "b=",
    ]  # fmt: skip


def test_upgrade_args_for_an_oci_chart_creating_its_namespace() -> None:
    chart = Chart(release="x", chart="oci://registry/x", version="v2", namespace="x", create_namespace=True)
    assert KUBE.upgrade_args(chart) == [
        "upgrade", "--install", "x", "oci://registry/x", "--version", "v2", "-n", "x", "--create-namespace",
    ]  # fmt: skip


def test_kubectl_is_pinned_to_the_lab_context() -> None:
    assert KUBE.kubectl_argv("llm") == ["kubectl", "--context", "lab-ctx", "-n", "llm"]


def test_context_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BONSAI_CONTEXT", "other")
    assert LabConfig.from_environment().kube_context == "other"


def test_every_values_file_exists() -> None:
    for chart in [infra.OLLAMA, infra.LITELLM, infra.CERT_MANAGER, infra.CLICKHOUSE_OPERATOR, infra.LANGFUSE]:
        if chart.values:
            assert CONFIG.infra_file(chart.values).is_file()


def test_missing_infra_file_says_where_to_run_from(tmp_path: Path) -> None:
    with pytest.raises(LabError, match="checkout"):
        LabConfig(root=tmp_path, kube_context="x").infra_file("nope.yaml")


def volume(name: str, namespace: str, path: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"claimRef": {"namespace": namespace, "name": "claim"}}
    if path is not None:
        spec["hostPath"] = {"path": path}
    return {"metadata": {"name": name}, "spec": spec}


def test_orphaned_volumes_are_the_lab_namespaces_pvs_and_their_minikube_directories() -> None:
    volumes = [
        volume("ollama", "llm", "/tmp/hostpath-provisioner/llm/ollama"),
        volume("keeper", "observability", "/tmp/hostpath-provisioner/observability/keeper"),
        volume("other-app", "default", "/tmp/hostpath-provisioner/default/data"),
        volume("not-hostpath", "llm"),
        volume("outside-root", "llm", "/var/lib/data"),
        {"metadata": {"name": "unclaimed"}, "spec": {"hostPath": {"path": "/tmp/hostpath-provisioner/x"}}},
    ]
    assert infra.orphaned_volumes(volumes) == (
        ["ollama", "keeper", "not-hostpath", "outside-root"],
        ["/tmp/hostpath-provisioner/llm/ollama", "/tmp/hostpath-provisioner/observability/keeper"],
    )
