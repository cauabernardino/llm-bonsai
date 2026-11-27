"""Thin wrappers around kubectl and helm, pinned to the lab's context, with failures turned into LabErrors.

Secrets only ever reach these commands on stdin, so a failing command can be shown in full.
"""

import json
import shlex
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from llm_bonsai.config import KUBE_CONTEXT, infra_file
from llm_bonsai.errors import LabError

GATEWAY_POD_LABELS = "app.kubernetes.io/name=litellm,app.kubernetes.io/component=proxy"


def run(*cmd: str, capture: bool = True, stdin: str | None = None) -> str:
    """Run a command and return its stdout.

    With capture=False the output goes straight to the terminal, so long installs show progress.
    """
    try:
        done = subprocess.run(cmd, check=True, text=True, capture_output=capture, input=stdin)
    except FileNotFoundError as error:
        raise LabError(f"{cmd[0]} not found on PATH") from error
    except subprocess.CalledProcessError as error:
        output = f"{error.stdout or ''}{error.stderr or ''}".strip()
        detail = f":\n{output[-1500:]}" if output else ""
        raise LabError(f"`{shlex.join(cmd)}` exited with {error.returncode}{detail}") from error
    return done.stdout if capture else ""


def kubectl_cmd(namespace: str | None = None) -> list[str]:
    scope = ["-n", namespace] if namespace else []
    return ["kubectl", "--context", KUBE_CONTEXT, *scope]


def kubectl(*args: str, namespace: str | None = None, stdin: str | None = None) -> str:
    return run(*kubectl_cmd(namespace), *args, stdin=stdin)


def kubectl_json(*args: str, namespace: str | None = None) -> Any:
    return json.loads(kubectl(*args, "-o", "json", namespace=namespace))


def apply_file(relative: str) -> None:
    """`kubectl apply` a manifest under infra/."""
    print(kubectl("apply", "-f", str(infra_file(relative))), end="", flush=True)


def apply_objects(objects: Iterable[dict[str, Any]]) -> None:
    """`kubectl apply` objects built in Python, so re-runs update instead of failing."""
    manifest = {"apiVersion": "v1", "kind": "List", "items": list(objects)}
    print(kubectl("apply", "-f", "-", stdin=json.dumps(manifest)), end="", flush=True)


def rollout_status(workload: str, namespace: str, timeout_s: int) -> None:
    kubectl("rollout", "status", workload, f"--timeout={timeout_s}s", namespace=namespace)


def wait_ready(namespace: str, timeout_s: int = 600, quiet: bool = False) -> None:
    """Block until every Deployment and StatefulSet in `namespace` has rolled out, under one deadline."""
    workloads = kubectl("get", "deployments,statefulsets", "-o", "name", namespace=namespace).split()
    if not workloads:
        raise LabError(f"no deployments or statefulsets in namespace {namespace!r}")
    deadline = time.monotonic() + timeout_s
    for workload in workloads:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise LabError(f"timed out before {namespace}/{workload} was ready")
        if not quiet:
            print(f"waiting for {namespace}/{workload} (up to {remaining}s)…", flush=True)
        rollout_status(workload, namespace, remaining)
    if not quiet:
        print(f"all {len(workloads)} workloads in {namespace} are ready")


def wait_for_crds(*crds: str) -> None:
    kubectl("wait", "--for=condition=Established", "--timeout=120s", *(f"crd/{crd}" for crd in crds))


def gateway_pods(include_terminating: bool = False) -> list[dict[str, Any]]:
    pods: list[dict[str, Any]] = kubectl_json("get", "pods", "-l", GATEWAY_POD_LABELS, namespace="llm")["items"]
    if include_terminating:
        return pods
    return [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]


@dataclass(frozen=True)
class Chart:
    """A pinned Helm release. `values` is relative to infra/."""

    release: str
    chart: str
    version: str
    namespace: str
    repo: str | None = None
    values: str | None = None
    create_namespace: bool = False
    wait_timeout: str | None = None  # helm --wait --timeout; None = don't wait

    def upgrade_args(self, set_values: Iterable[str] = ()) -> list[str]:
        args = ["helm", "upgrade", "--install", self.release, self.chart, "--version", self.version]
        args += ["--kube-context", KUBE_CONTEXT, "-n", self.namespace]
        if self.repo:
            args += ["--repo", self.repo]
        if self.values:
            args += ["-f", str(infra_file(self.values))]
        if self.create_namespace:
            args.append("--create-namespace")
        if self.wait_timeout:
            args += ["--wait", "--timeout", self.wait_timeout]
        for value in set_values:
            args += ["--set", value]
        return args


def helm(*args: str) -> str:
    return run("helm", "--kube-context", KUBE_CONTEXT, *args)


def helm_upgrade(chart: Chart, set_values: Iterable[str] = (), quiet: bool = False) -> None:
    run(*chart.upgrade_args(set_values), capture=quiet)
