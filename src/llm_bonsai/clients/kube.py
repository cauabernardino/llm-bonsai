"""kubectl and helm, pinned to the lab's context."""

import json
import time
from collections.abc import Iterable
from typing import Any

from llm_bonsai.clients.shell import run
from llm_bonsai.config import LabConfig
from llm_bonsai.errors import LabError
from llm_bonsai.models import Chart


class Kube:
    def __init__(self, config: LabConfig) -> None:
        self.config = config

    def kubectl_argv(self, namespace: str | None = None) -> list[str]:
        scope = ["-n", namespace] if namespace else []
        return ["kubectl", "--context", self.config.kube_context, *scope]

    def kubectl(self, *args: str, namespace: str | None = None, stdin: str | None = None) -> str:
        return run(*self.kubectl_argv(namespace), *args, stdin=stdin)

    def kubectl_json(self, *args: str, namespace: str | None = None) -> Any:
        return json.loads(self.kubectl(*args, "-o", "json", namespace=namespace))

    def apply_file(self, relative: str) -> None:
        """`kubectl apply` a manifest under infra/."""
        print(self.kubectl("apply", "-f", str(self.config.infra_file(relative))), end="", flush=True)

    def apply_objects(self, objects: Iterable[dict[str, Any]]) -> None:
        """`kubectl apply` objects built in Python, so re-runs update instead of failing."""
        manifest = {"apiVersion": "v1", "kind": "List", "items": list(objects)}
        print(self.kubectl("apply", "-f", "-", stdin=json.dumps(manifest)), end="", flush=True)

    def rollout_status(self, workload: str, namespace: str, timeout_s: int) -> None:
        self.kubectl("rollout", "status", workload, f"--timeout={timeout_s}s", namespace=namespace)

    def wait_ready(self, namespace: str, timeout_s: int = 600, quiet: bool = False) -> None:
        """Block until every Deployment and StatefulSet in `namespace` has rolled out, under one deadline."""
        workloads = self.kubectl("get", "deployments,statefulsets", "-o", "name", namespace=namespace).split()
        if not workloads:
            raise LabError(f"no deployments or statefulsets in namespace {namespace!r}")
        deadline = time.monotonic() + timeout_s
        for workload in workloads:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise LabError(f"timed out before {namespace}/{workload} was ready")
            if not quiet:
                print(f"waiting for {namespace}/{workload} (up to {remaining}s)…", flush=True)
            self.rollout_status(workload, namespace, remaining)
        if not quiet:
            print(f"all {len(workloads)} workloads in {namespace} are ready")

    def wait_for_crds(self, *crds: str) -> None:
        self.kubectl("wait", "--for=condition=Established", "--timeout=120s", *(f"crd/{crd}" for crd in crds))

    def helm(self, *args: str, capture: bool = True) -> str:
        return run("helm", "--kube-context", self.config.kube_context, *args, capture=capture)

    def upgrade_args(self, chart: Chart, set_values: Iterable[str] = ()) -> list[str]:
        """The `helm upgrade --install` arguments (after `helm --kube-context …`) for a chart."""
        args = ["upgrade", "--install", chart.release, chart.chart, "--version", chart.version, "-n", chart.namespace]
        if chart.repo:
            args += ["--repo", chart.repo]
        if chart.values:
            args += ["-f", str(self.config.infra_file(chart.values))]
        if chart.create_namespace:
            args.append("--create-namespace")
        if chart.wait_timeout:
            args += ["--wait", "--timeout", chart.wait_timeout]
        for value in set_values:
            args += ["--set", value]
        return args

    def helm_upgrade(self, chart: Chart, set_values: Iterable[str] = (), quiet: bool = False) -> None:
        self.helm(*self.upgrade_args(chart, set_values), capture=quiet)
