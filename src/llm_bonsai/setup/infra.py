"""The cluster and everything installed into it, one method per step.

Every step is idempotent (`helm upgrade --install`, `kubectl apply`), so any of them,
or `install_all`, can be re-run to converge the cluster on what is in infra/.
"""

from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from llm_bonsai.catalog import LLM_NAMESPACE, OBSERVABILITY_NAMESPACE
from llm_bonsai.clients.kube import Kube
from llm_bonsai.clients.shell import run
from llm_bonsai.config import EnvFile
from llm_bonsai.errors import LabError
from llm_bonsai.models import Chart
from llm_bonsai.setup import credentials

OLLAMA = Chart(
    release="ollama",
    chart="ollama",
    repo="https://helm.otwld.com/",
    version="1.96.0",
    namespace=LLM_NAMESPACE,
    values="ollama/values.yaml",
)
LITELLM = Chart(
    release="litellm",
    chart="oci://ghcr.io/berriai/litellm/chart/litellm",
    version="1.111.0",
    namespace=LLM_NAMESPACE,
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
    namespace=OBSERVABILITY_NAMESPACE,
    values="langfuse/values.yaml",
)

CLUSTER_CPUS = 6
CLUSTER_MEMORY_GIB = 14
CLUSTER_DISK_GB = 40

# Where minikube's storage-provisioner keeps each volume: <root><namespace>/<claim name>.
HOSTPATH_ROOT = "/tmp/hostpath-provisioner/"  # noqa: S108 (a path on the node, not a local temp file)


def orphaned_volumes(volumes: Iterable[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Names of the PVs once claimed from the lab's namespaces, and the node directories holding their data.

    Only meaningful after every claim in those namespaces is deleted. minikube's provisioner
    stamps each PV with an identity it picks at random on startup and deletes only PVs carrying
    its current one, so after a restart a deleted claim leaves its PV Released and its data on
    the node. A new claim with the same name gets the same directory, stale data included.
    """
    names: list[str] = []
    paths: list[str] = []
    for volume in volumes:
        if volume["spec"].get("claimRef", {}).get("namespace") not in (LLM_NAMESPACE, OBSERVABILITY_NAMESPACE):
            continue
        names.append(volume["metadata"]["name"])
        path = volume["spec"].get("hostPath", {}).get("path", "")
        if path.startswith(HOSTPATH_ROOT):
            paths.append(path)
    return names, paths


def create_cluster() -> None:
    # Docker Desktop's VM needs >= 17 GB: minikube's 14 GiB plus headroom for the VM itself.
    run(
        "minikube", "start", "--driver=docker", f"--cpus={CLUSTER_CPUS}",
        f"--memory={CLUSTER_MEMORY_GIB}g", f"--disk-size={CLUSTER_DISK_GB}g",
        capture=False,
    )  # fmt: skip
    run("minikube", "addons", "enable", "metrics-server", capture=False)


def delete_cluster() -> None:
    run("minikube", "delete", capture=False)


class Installer:
    def __init__(self, kube: Kube, env: EnvFile) -> None:
        self._kube = kube
        self._env = env

    def install_namespaces(self) -> None:
        self._kube.apply_file("namespaces.yaml")

    def install_secrets(self) -> None:
        added = self._env.fill_missing(credentials.GENERATORS)
        print(f"{self._env.path.name}: added {', '.join(added) if added else 'nothing (all present)'}")
        self._kube.apply_objects(credentials.kubernetes_secrets(self._env))

    def install_ollama(self) -> None:
        self._kube.helm_upgrade(OLLAMA)
        self._kube.wait_ready(LLM_NAMESPACE, timeout_s=900)

    def install_postgres(self) -> None:
        self._kube.apply_file("postgres/postgres.yaml")
        self._kube.rollout_status("statefulset/postgres", LLM_NAMESPACE, timeout_s=180)

    def install_valkey(self) -> None:
        self._kube.apply_file("valkey/valkey.yaml")
        self._kube.rollout_status("deployment/valkey", LLM_NAMESPACE, timeout_s=120)

    def install_cert_manager(self) -> None:
        self._kube.helm_upgrade(CERT_MANAGER)
        self._kube.wait_for_crds("certificates.cert-manager.io", "issuers.cert-manager.io")

    def install_clickhouse_operator(self) -> None:
        self._kube.helm_upgrade(CLICKHOUSE_OPERATOR)
        self._kube.wait_for_crds("clickhouseclusters.clickhouse.com", "keeperclusters.clickhouse.com")

    def install_langfuse(self) -> None:
        self._kube.helm_upgrade(LANGFUSE)
        self._kube.wait_ready(OBSERVABILITY_NAMESPACE, timeout_s=900)

    def install_prometheus(self, scrape_key: str) -> None:
        """Optional, so not part of `install_all`. The gateway must be up to issue `scrape_key`."""
        self._kube.apply_objects(
            [credentials.secret("prometheus-litellm", OBSERVABILITY_NAMESPACE, {"key": scrape_key})]
        )
        self._kube.apply_file("prometheus/prometheus.yaml")
        self._kube.rollout_status("deployment/prometheus", OBSERVABILITY_NAMESPACE, timeout_s=180)

    def install_litellm(self, set_values: Iterable[str] = (), quiet: bool = False) -> None:
        """Postgres must be up first: migrations run as a pre-install hook.

        `set_values` are one-off `helm --set` overrides on top of values.yaml; a later run
        without them puts the gateway back to normal.
        """
        self._kube.helm_upgrade(LITELLM, set_values, quiet=quiet)
        self._kube.wait_ready(LLM_NAMESPACE, quiet=quiet)

    def install_all(self) -> None:
        # Langfuse goes before LiteLLM so the gateway's first requests are traced.
        steps: list[tuple[str, Callable[[], None]]] = [
            ("namespaces", self.install_namespaces),
            ("secrets", self.install_secrets),
            ("ollama", self.install_ollama),
            ("postgres", self.install_postgres),
            ("valkey", self.install_valkey),
            ("cert-manager", self.install_cert_manager),
            ("clickhouse-operator", self.install_clickhouse_operator),
            ("langfuse", self.install_langfuse),
            ("litellm", self.install_litellm),
        ]
        for number, (name, step) in enumerate(steps, start=1):
            print(f"\n▶ [{number}/{len(steps)}] {name}", flush=True)
            step()
        print("\nNext: `llm-bonsai setup teams`, then `llm-bonsai probe smoke`.")

    def uninstall(self) -> None:
        """Remove the lab's releases and data, keeping the cluster and its cluster-wide
        prerequisites (cert-manager, the ClickHouse operator). Missing pieces are skipped."""
        helm, kubectl = self._kube.helm, self._kube.kubectl
        manifests = [str(self._kube.config.infra_file(f)) for f in ("valkey/valkey.yaml", "postgres/postgres.yaml")]
        prometheus = str(self._kube.config.infra_file("prometheus/prometheus.yaml"))
        steps: list[Callable[[], str]] = [
            partial(kubectl, "delete", "--ignore-not-found", "-f", prometheus),
            partial(helm, "uninstall", LANGFUSE.release, "-n", LANGFUSE.namespace, "--wait"),
            partial(kubectl, "delete", "pvc", "--all", namespace=OBSERVABILITY_NAMESPACE),
            partial(helm, "uninstall", LITELLM.release, "-n", LITELLM.namespace),
            partial(helm, "uninstall", OLLAMA.release, "-n", OLLAMA.namespace),
            partial(kubectl, "delete", "--ignore-not-found", "-f", manifests[0], "-f", manifests[1]),
            partial(kubectl, "delete", "pvc", "--all", namespace=LLM_NAMESPACE),
            self.delete_orphaned_volumes,
        ]
        for step in steps:
            try:
                print(step(), end="", flush=True)
            except LabError as error:
                print(f"skipped: {error}")

    def delete_orphaned_volumes(self) -> str:
        """Delete what the provisioner may have left behind (see `orphaned_volumes`)."""
        names, paths = orphaned_volumes(self._kube.kubectl_json("get", "pv")["items"])
        if not names:
            return "no volumes left behind\n"
        self._kube.kubectl("delete", "pv", "--ignore-not-found", *names)
        if paths:
            run("minikube", "ssh", "--", "sudo", "rm", "-rf", *paths)
        return f"deleted {len(names)} leftover volumes and {len(paths)} data directories on the node\n"
