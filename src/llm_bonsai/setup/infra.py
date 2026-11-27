"""The cluster and everything installed into it, one function per step.

Every step is idempotent (`helm upgrade --install`, `kubectl apply`), so any of them,
or `install_all`, can be re-run to converge the cluster on what is in infra/.
"""

from collections.abc import Callable, Iterable

from llm_bonsai import kube
from llm_bonsai.config import infra_file
from llm_bonsai.errors import LabError
from llm_bonsai.kube import Chart
from llm_bonsai.setup import credentials

OLLAMA = Chart(
    release="ollama",
    chart="ollama",
    repo="https://helm.otwld.com/",
    version="1.96.0",
    namespace="llm",
    values="ollama/values.yaml",
)
LITELLM = Chart(
    release="litellm",
    chart="oci://ghcr.io/berriai/litellm/chart/litellm",
    version="1.111.0",
    namespace="llm",
    values="litellm/values.yaml",
    wait_timeout="10m",
)
# Cluster-wide prerequisites of the Langfuse chart: the ClickHouse operator renders
# ClickHouseCluster/KeeperCluster CRs and gets its webhook certificates from cert-manager.
CERT_MANAGER = Chart(
    release="cert-manager",
    chart="oci://quay.io/jetstack/charts/cert-manager",
    version="v1.21.3",
    namespace="cert-manager",
    values="cert-manager/values.yaml",
    create_namespace=True,
    wait_timeout="5m",
)
CLICKHOUSE_OPERATOR = Chart(
    release="clickhouse-operator",
    chart="oci://ghcr.io/clickhouse/clickhouse-operator-helm",
    version="0.0.8",
    namespace="clickhouse-operator",
    create_namespace=True,
    wait_timeout="5m",
)
# The release name must be `langfuse`: service names (langfuse-web, ...) derive from it.
LANGFUSE = Chart(
    release="langfuse",
    chart="langfuse",
    repo="https://langfuse.github.io/langfuse-k8s",
    version="2.3.0",
    namespace="observability",
    values="langfuse/values.yaml",
)

CLUSTER_CPUS = 6
CLUSTER_MEMORY_GIB = 14
CLUSTER_DISK_GB = 40


def create_cluster() -> None:
    """Start minikube with 6 CPUs, 14 GiB and 40 GB of disk, plus metrics-server."""
    # Docker Desktop's VM needs >= 17 GB: minikube's 14 GiB plus headroom for the VM itself.
    kube.run(
        "minikube", "start", "--driver=docker", f"--cpus={CLUSTER_CPUS}",
        f"--memory={CLUSTER_MEMORY_GIB}g", f"--disk-size={CLUSTER_DISK_GB}g",
        capture=False,
    )  # fmt: skip
    kube.run("minikube", "addons", "enable", "metrics-server", capture=False)


def delete_cluster() -> None:
    kube.run("minikube", "delete", capture=False)


def install_namespaces() -> None:
    """Create the llm and observability namespaces."""
    kube.apply_file("namespaces.yaml")


def install_secrets() -> None:
    """Fill .env with random dev secrets (keeping existing ones) and apply them as Kubernetes Secrets."""
    credentials.apply()


def install_ollama() -> None:
    """Install Ollama and pull qwen2.5:0.5b and nomic-embed-text (first run: a few minutes)."""
    kube.helm_upgrade(OLLAMA)
    kube.wait_ready("llm", timeout_s=900)


def install_postgres() -> None:
    """Install the gateway's Postgres (keys, teams, spend)."""
    kube.apply_file("postgres/postgres.yaml")
    kube.rollout_status("statefulset/postgres", "llm", timeout_s=180)


def install_valkey() -> None:
    """Install Valkey, the gateway's shared cache and rate-limit store."""
    kube.apply_file("valkey/valkey.yaml")
    kube.rollout_status("deployment/valkey", "llm", timeout_s=120)


def install_cert_manager() -> None:
    """Install cert-manager, a prerequisite of the ClickHouse operator."""
    kube.helm_upgrade(CERT_MANAGER)
    kube.wait_for_crds("certificates.cert-manager.io", "issuers.cert-manager.io")


def install_clickhouse_operator() -> None:
    """Install the ClickHouse operator, a prerequisite of the Langfuse chart."""
    kube.helm_upgrade(CLICKHOUSE_OPERATOR)
    kube.wait_for_crds("clickhouseclusters.clickhouse.com", "keeperclusters.clickhouse.com")


def install_langfuse() -> None:
    """Install Langfuse v4 with ClickHouse, Postgres, Valkey and SeaweedFS (first run: up to 15 min)."""
    kube.helm_upgrade(LANGFUSE)
    kube.wait_ready("observability", timeout_s=900)


def install_litellm(set_values: Iterable[str] = (), quiet: bool = False) -> None:
    """Install the LiteLLM gateway. Postgres must be up first: migrations run as a pre-install hook.

    `set_values` are one-off `helm --set` overrides on top of values.yaml; a later run
    without them puts the gateway back to normal.
    """
    kube.helm_upgrade(LITELLM, set_values, quiet=quiet)
    kube.wait_ready("llm", quiet=quiet)


# The order `install_all` runs. Langfuse goes before LiteLLM so the gateway's first requests are traced.
INSTALL_ORDER: list[tuple[str, Callable[[], None]]] = [
    ("namespaces", install_namespaces),
    ("secrets", install_secrets),
    ("ollama", install_ollama),
    ("postgres", install_postgres),
    ("valkey", install_valkey),
    ("cert-manager", install_cert_manager),
    ("clickhouse-operator", install_clickhouse_operator),
    ("langfuse", install_langfuse),
    ("litellm", install_litellm),
]


def install_all() -> None:
    """Run every install step, namespaces through litellm, in order (not `cluster` or `teams`)."""
    for number, (name, step) in enumerate(INSTALL_ORDER, start=1):
        print(f"\n▶ [{number}/{len(INSTALL_ORDER)}] {name}", flush=True)
        step()
    print("\nNext: `llm-bonsai setup teams`, then `llm-bonsai probe smoke`.")


def uninstall() -> None:
    """Remove the lab's releases and data, keeping the cluster and its cluster-wide
    prerequisites (cert-manager, the ClickHouse operator). Missing pieces are skipped."""
    manifests = [str(infra_file("valkey/valkey.yaml")), str(infra_file("postgres/postgres.yaml"))]
    steps: list[tuple[str, ...]] = [
        ("helm", "uninstall", "langfuse", "-n", "observability", "--wait"),
        ("kubectl", "delete", "pvc", "--all", "-n", "observability"),
        ("helm", "uninstall", "litellm", "-n", "llm"),
        ("helm", "uninstall", "ollama", "-n", "llm"),
        ("kubectl", "delete", "--ignore-not-found", "-f", manifests[0], "-f", manifests[1]),
        ("kubectl", "delete", "pvc", "--all", "-n", "llm"),
    ]
    for tool, *args in steps:
        try:
            output = kube.helm(*args) if tool == "helm" else kube.kubectl(*args)
            print(output, end="", flush=True)
        except LabError as error:
            print(f"skipped: {error}")
