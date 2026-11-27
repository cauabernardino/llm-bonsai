import pytest

from llm_bonsai.config import INFRA, KUBE_CONTEXT, infra_file
from llm_bonsai.errors import LabError
from llm_bonsai.kube import Chart, kubectl_cmd
from llm_bonsai.setup import infra


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
    assert chart.upgrade_args(["a=1", "b="]) == [
        "helm", "upgrade", "--install", "ollama", "ollama", "--version", "1.0.0",
        "--kube-context", KUBE_CONTEXT, "-n", "llm", "--repo", "https://example.test/",
        "-f", str(INFRA / "ollama/values.yaml"), "--wait", "--timeout", "10m", "--set", "a=1", "--set", "b=",
    ]  # fmt: skip


def test_upgrade_args_for_an_oci_chart_creating_its_namespace() -> None:
    chart = Chart(release="x", chart="oci://registry/x", version="v2", namespace="x", create_namespace=True)
    assert chart.upgrade_args() == [
        "helm", "upgrade", "--install", "x", "oci://registry/x", "--version", "v2",
        "--kube-context", KUBE_CONTEXT, "-n", "x", "--create-namespace",
    ]  # fmt: skip


def test_kubectl_is_pinned_to_the_lab_context() -> None:
    assert kubectl_cmd("llm") == ["kubectl", "--context", KUBE_CONTEXT, "-n", "llm"]


def test_every_values_file_exists() -> None:
    for chart in [infra.OLLAMA, infra.LITELLM, infra.CERT_MANAGER, infra.CLICKHOUSE_OPERATOR, infra.LANGFUSE]:
        if chart.values:
            assert infra_file(chart.values).is_file()


def test_missing_infra_file_says_where_to_run_from() -> None:
    with pytest.raises(LabError, match="checkout"):
        infra_file("nope.yaml")
