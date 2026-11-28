from pathlib import Path
from typing import Any

import pytest

from llm_bonsai.clients.kube import Kube
from llm_bonsai.config import CHECKOUT, INFRA, LabConfig, infra_file, lab_home
from llm_bonsai.errors import LabError
from llm_bonsai.models import Chart
from llm_bonsai.setup import infra

CONFIG = LabConfig.from_environment()
KUBE = Kube(LabConfig(home=CONFIG.home, kube_context="lab-ctx"))


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
        "--repo", "https://example.test/", "-f", str(INFRA / "ollama/values.yaml"),
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


def test_every_infra_file_ships_inside_the_package() -> None:
    charts = [infra.OLLAMA, infra.LITELLM, infra.CERT_MANAGER, infra.CLICKHOUSE_OPERATOR, infra.LANGFUSE]
    manifests = ["namespaces.yaml", "postgres/postgres.yaml", "valkey/valkey.yaml", "prometheus/prometheus.yaml"]
    for relative in [*(chart.values for chart in charts if chart.values), *manifests]:
        assert infra_file(relative).is_relative_to(INFRA)


def test_missing_infra_file_says_to_reinstall() -> None:
    with pytest.raises(LabError, match="reinstall"):
        infra_file("nope.yaml")


def test_lab_home_prefers_llm_bonsai_home(tmp_path: Path) -> None:
    assert lab_home({"LLM_BONSAI_HOME": str(tmp_path)}) == tmp_path


def test_lab_home_is_the_checkout_when_running_from_one() -> None:
    # The tests themselves run from the checkout.
    assert lab_home({}) == CHECKOUT


def test_lab_home_of_an_installed_package_is_the_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("llm_bonsai.config.CHECKOUT", tmp_path / "site-packages-parent")
    assert lab_home({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "llm-bonsai"


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


def namespace(name: str, labels: dict[str, str] | None = None) -> dict[str, Any]:
    return {"metadata": {"name": name, **({"labels": labels} if labels is not None else {})}}


def test_teardown_only_empties_namespaces_llm_bonsai_created() -> None:
    owned = namespace("llm", {"app.kubernetes.io/part-of": "llm-bonsai"})
    unlabelled = namespace("observability")
    someone_elses = namespace("observability", {"app.kubernetes.io/part-of": "their-stack"})
    assert infra.foreign_namespaces([owned]) == []
    assert infra.foreign_namespaces([owned, unlabelled]) == ["observability"]
    assert infra.foreign_namespaces([someone_elses]) == ["observability"]


def test_the_lab_uses_its_own_minikube_profile_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BONSAI_CONTEXT", raising=False)
    config = LabConfig.from_environment()
    assert config.kube_context == config.minikube_profile == "llm-bonsai"


def test_every_minikube_command_names_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def record(*cmd: str, **kwargs: object) -> str:
        calls.append(cmd)
        return ""

    monkeypatch.setattr(infra, "run", record)
    infra.create_cluster("lab")
    infra.delete_cluster("lab")
    assert len(calls) == 3
    assert all(call[0] == "minikube" and call[call.index("-p") + 1] == "lab" for call in calls)


def test_a_missing_context_points_at_setup_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_bonsai.clients.kube.run", lambda *cmd, **kwargs: "minikube\nkind-dev\n")
    with pytest.raises(LabError, match="setup cluster") as raised:
        KUBE.require_context()
    assert "LLM_BONSAI_CONTEXT=minikube" in str(raised.value)
