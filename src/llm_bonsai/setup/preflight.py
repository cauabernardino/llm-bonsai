"""Check the host against the lab's hard requirements, then say what to do next.

Read-only: it never installs or starts anything. Supports macOS and Linux (WSL2 counts as
Linux); on anything else it reports the OS check as failed instead of crashing.
"""

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from rich.text import Text

from llm_bonsai import report
from llm_bonsai.setup.infra import CLUSTER_CPUS, CLUSTER_DISK_GB, CLUSTER_MEMORY_GIB, KUBERNETES_VERSION

GIB = 1024**3
SUPPORTED_SYSTEMS = {"Darwin": "macOS", "Linux": "Linux"}
SUPPORTED_ARCHS = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
# Docker needs room for its own processes and image unpacking on top of the cluster's limit.
MIN_DOCKER_MEMORY_GIB = CLUSTER_MEMORY_GIB + 2.5
# Docker Desktop's memory setting in GB shows up as a bit less MemTotal: 17 GB reads as ~16.6 GiB.
DOCKER_DESKTOP_MEMORY_GB = 17
# What the host OS keeps for itself: next to Docker Desktop's VM, or next to the cluster on native Docker.
MIN_HOST_HEADROOM_GIB = 4
MIN_FREE_DISK_GIB = CLUSTER_DISK_GB
# x86 (AVX-512 BF16, AMX) and arm64 spellings in /proc/cpuinfo.
BF16_FLAGS = {"avx512_bf16", "amx_bf16", "bf16"}


@dataclass(frozen=True)
class Tool:
    name: str
    command: list[str]
    minimum: tuple[int, int]
    why: str


_major, _minor = KUBERNETES_VERSION.removeprefix("v").split(".")[:2]
SERVER_MINOR = (int(_major), int(_minor))
# kubectl is supported within one minor version of the server; minikube 1.39 is the first to run 1.37.
TOOLS = [
    Tool("kubectl", ["kubectl", "version", "--client", "-o", "json"], (SERVER_MINOR[0], SERVER_MINOR[1] - 1),
         f"±1 minor of Kubernetes {SERVER_MINOR[0]}.{SERVER_MINOR[1]}"),
    Tool("helm", ["helm", "version", "--short"], (3, 8), "OCI chart registries"),
    Tool("minikube", ["minikube", "version", "--short"], (1, 39), f"runs Kubernetes {KUBERNETES_VERSION}"),
]  # fmt: skip
LINUX_DOCS = {
    "kubectl": "https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/",
    "helm": "https://helm.sh/docs/intro/install/",
    "minikube": "https://minikube.sigs.k8s.io/docs/start/",
    "docker": "https://docs.docker.com/engine/install/",
}
BREW_FORMULA = {"kubectl": "kubernetes-cli", "helm": "helm", "minikube": "minikube"}


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Docker:
    memory_gib: float
    cpus: int
    version: str
    in_vm: bool  # Docker Desktop: containers share a VM's memory, not the host's
    root_dir: str


@dataclass(frozen=True)
class Host:
    system: str  # platform.system()
    wsl: bool  # Linux inside WSL2: memory is set in .wslconfig, and Windows' own RAM isn't visible
    os_name: str
    arch: str  # platform.machine()
    cpu: str
    bf16: bool | None  # None when the CPU can't be identified
    ram_gib: float | None  # None where the OS doesn't expose it to Python
    docker: Docker | None
    docker_cli: bool
    free_disk_gib: float
    disk_path: str
    tools: dict[str, str | None]  # raw version output per tool name
    current_context: str | None
    orbstack: bool


@dataclass(frozen=True)
class Check:
    name: str
    found: str
    needed: str
    status: Status
    fix: str | None = None


def output_of(cmd: list[str]) -> str | None:
    """Stripped stdout of a command, or None if it is missing or fails."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip()


def tool_version(tool: Tool) -> str | None:
    out = output_of(tool.command)
    if out is None:
        return None
    if tool.name != "kubectl":
        return out.splitlines()[0] if out else None
    try:
        return str(json.loads(out)["clientVersion"]["gitVersion"])
    except (ValueError, KeyError, TypeError):
        return None


def parse_version(text: str) -> tuple[int, int] | None:
    """(major, minor) from output like 'v1.37.3', 'v4.3.2+g1a2b3c' or 'minikube version: v1.39.0'."""
    found = re.search(r"v?(\d+)\.(\d+)", text)
    return (int(found.group(1)), int(found.group(2))) if found else None


def docker_info(system: str) -> Docker | None:
    out = output_of(["docker", "info", "--format", "{{json .}}"])
    if out is None:
        return None
    try:
        info = json.loads(out)
    except ValueError:
        return None
    return parse_docker_info(info, system) if isinstance(info, dict) else None


def parse_docker_info(info: dict[str, Any], system: str) -> Docker | None:
    # The CLI still prints JSON, with ServerErrors, when it can't reach the daemon.
    if info.get("ServerErrors") or not info.get("MemTotal"):
        return None
    return Docker(
        memory_gib=info["MemTotal"] / GIB,
        cpus=int(info.get("NCPU", 0)),
        version=str(info.get("ServerVersion", "?")),
        in_vm=system == "Darwin" or "Docker Desktop" in str(info.get("OperatingSystem", "")),
        root_dir=str(info.get("DockerRootDir", "/")),
    )


def os_name(system: str) -> str:
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if system == "Linux":
        try:
            return platform.freedesktop_os_release().get("PRETTY_NAME", f"Linux {platform.release()}")
        except OSError:
            return f"Linux {platform.release()}"
    return f"{system} {platform.release()}"


def cpu_facts(system: str) -> tuple[str, bool | None]:
    """The CPU model, and whether it has the bf16 instructions vLLM's CPU backend wants."""
    if system == "Darwin":
        model = output_of(["sysctl", "-n", "machdep.cpu.brand_string"]) or "unknown"
        if not model.startswith("Apple"):
            return model, None
        return model, "M1" not in model  # M2 and newer have bf16
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
    except OSError:
        return platform.processor() or platform.machine() or "unknown", None
    name, bf16 = parse_cpuinfo(cpuinfo)
    return name or platform.processor() or platform.machine() or "unknown", bf16


def parse_cpuinfo(cpuinfo: str) -> tuple[str | None, bool | None]:
    """The model name (absent on most arm64 kernels) and bf16 support, from /proc/cpuinfo."""
    model = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    flags = re.search(r"^(?:flags|Features)\s*:\s*(.*)$", cpuinfo, re.MULTILINE)
    bf16 = bool(set(flags.group(1).split()) & BF16_FLAGS) if flags else None
    return (model.group(1).strip() if model else None), bf16


def total_ram_gib() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GIB
    except (AttributeError, ValueError, OSError):  # no sysconf on Windows
        return None


def disk_to_check(docker: Docker | None) -> Path:
    """Where Docker keeps images and volumes: inside its VM image under the home directory
    with Docker Desktop, its data root on the host otherwise."""
    if docker is None or docker.in_vm:
        return Path.home()
    root = Path(docker.root_dir)
    return root if root.exists() else Path("/")


def collect() -> Host:
    system = platform.system()
    docker = docker_info(system)
    disk = disk_to_check(docker)
    cpu, bf16 = cpu_facts(system)
    return Host(
        system=system,
        wsl=system == "Linux" and "microsoft" in platform.release().lower(),
        os_name=os_name(system),
        arch=platform.machine(),
        cpu=cpu,
        bf16=bf16,
        ram_gib=total_ram_gib(),
        docker=docker,
        docker_cli=shutil.which("docker") is not None,
        free_disk_gib=shutil.disk_usage(disk).free / GIB,
        disk_path=str(disk),
        tools={tool.name: tool_version(tool) for tool in TOOLS},
        current_context=output_of(["kubectl", "config", "current-context"]),
        orbstack=shutil.which("orbctl") is not None,
    )


def install_hint(host: Host, name: str, upgrade: bool = False) -> str | None:
    if host.system == "Darwin":
        if name == "docker":
            return "install Docker Desktop: https://docs.docker.com/desktop/"
        return f"brew {'upgrade' if upgrade else 'install'} {BREW_FORMULA[name]}"
    if host.system == "Linux":
        return LINUX_DOCS[name]
    return None


def requirement(name: str, found: str, needed: str, met: bool, fix: str | None = None, soft: bool = False) -> Check:
    status = Status.OK if met else Status.WARN if soft else Status.FAIL
    return Check(name, found, needed, status, None if met else fix)


def resources_fix(host: Host, docker: Docker, memory: bool) -> str:
    """Where to give Docker more memory (or CPUs) on this kind of host."""
    if host.wsl:
        key = "memory" if memory else "processors"
        return f"raise `{key}=` under [wsl2] in %UserProfile%\\.wslconfig, then `wsl --shutdown`"
    if docker.in_vm:
        setting = f"Memory ≥ {DOCKER_DESKTOP_MEMORY_GB} GB" if memory else "CPUs"
        return f"Docker Desktop → Settings → Resources → {setting}"
    return f"use a machine with more {'RAM' if memory else 'CPUs'}"


def docker_checks(host: Host, docker: Docker) -> list[Check]:
    results = [
        requirement("Docker", docker.version, "running", True),
        requirement("Memory for Docker", f"{docker.memory_gib:.1f} GiB", f"≥ {MIN_DOCKER_MEMORY_GIB:g} GiB",
                    docker.memory_gib >= MIN_DOCKER_MEMORY_GIB, resources_fix(host, docker, memory=True)),
        requirement("CPUs for Docker", str(docker.cpus), f"≥ {CLUSTER_CPUS}", docker.cpus >= CLUSTER_CPUS,
                    resources_fix(host, docker, memory=False)),
    ]  # fmt: skip
    # Inside WSL2 the Windows host's RAM isn't visible, so its headroom can't be judged.
    if host.ram_gib is not None and not host.wsl:
        taken = docker.memory_gib if docker.in_vm else CLUSTER_MEMORY_GIB
        headroom = host.ram_gib - taken
        results.append(
            requirement("RAM left for the host", f"{headroom:.1f} GiB of {host.ram_gib:.0f}",
                        f"≥ {MIN_HOST_HEADROOM_GIB} GiB", headroom >= MIN_HOST_HEADROOM_GIB,
                        "the host will swap while the lab runs; close other apps", soft=True)
        )  # fmt: skip
    return results


def checks(host: Host) -> list[Check]:
    """The hard requirements (FAIL blocks the lab) and the soft ones (WARN)."""
    results = [
        requirement("Operating system", host.os_name, "macOS or Linux", host.system in SUPPORTED_SYSTEMS,
                    "on Windows, run it inside WSL2 (a Linux distribution)"),
        requirement("CPU architecture", host.arch, "arm64 or amd64", host.arch in SUPPORTED_ARCHS),
    ]  # fmt: skip

    if host.docker is not None:
        results += docker_checks(host, host.docker)
    else:
        if not host.docker_cli:
            fix = install_hint(host, "docker")
        elif host.system == "Darwin":
            fix = "start Docker Desktop"
        elif host.wsl:
            fix = (
                "start Docker Desktop on Windows with WSL integration for this distribution, or Docker Engine inside it"
            )
        else:
            fix = "start it (`sudo systemctl start docker`) and run it without sudo (`sudo usermod -aG docker $USER`)"
        results.append(requirement("Docker", "not running" if host.docker_cli else "missing", "running", False, fix))

    results.append(
        requirement("Free disk", f"{host.free_disk_gib:.0f} GiB at {host.disk_path}", f"≥ {MIN_FREE_DISK_GIB} GiB",
                    host.free_disk_gib >= MIN_FREE_DISK_GIB, "free up space where Docker keeps its data")
    )  # fmt: skip

    for tool in TOOLS:
        raw = host.tools.get(tool.name)
        version = parse_version(raw) if raw else None
        needed = f"≥ {tool.minimum[0]}.{tool.minimum[1]} ({tool.why})"
        if version is None:
            results.append(requirement(tool.name, "missing", needed, False, install_hint(host, tool.name)))
        else:
            results.append(
                requirement(tool.name, f"{version[0]}.{version[1]}", needed, version >= tool.minimum,
                            install_hint(host, tool.name, upgrade=True))
            )  # fmt: skip
    return results


STATUS_TEXT = {
    Status.OK: Text("✓", style="bold green"),
    Status.WARN: Text("!", style="bold yellow"),
    Status.FAIL: Text("✗", style="bold red"),
}


def render(host: Host, results: list[Check]) -> None:
    rows = report.table("", "check", "found", "needs", title="Requirements")
    for check in results:
        rows.add_row(STATUS_TEXT[check.status], check.name, check.found, check.needed)
    report.console.print(rows)

    bf16 = {True: "yes", False: "no", None: "unknown"}[host.bf16]
    report.pairs(
        ("CPU", host.cpu),
        ("bf16", f"{bf16} (what vLLM on CPU wants, for the optional vLLM phase)"),
        ("Host RAM", f"{host.ram_gib:.0f} GiB" if host.ram_gib is not None else "unknown"),
        ("kube context", host.current_context or "none"),
    )

    problems = [check for check in results if check.status is not Status.OK]
    if any(check.status is Status.FAIL for check in results):
        todo = [f"{check.name}: {check.fix or 'see the needs column'}" for check in problems]
        report.show(report.panel("Fix these first", report.bullets(todo), style="red"))
        return
    nexts = [
        f"Create the cluster: `llm-bonsai setup cluster` (minikube on Docker, {CLUSTER_CPUS} CPUs, "
        f"{CLUSTER_MEMORY_GIB} GiB, {CLUSTER_DISK_GB} GB of disk)",
        *(f"{check.name}: {check.fix}" for check in problems if check.fix),
    ]
    if host.orbstack:
        nexts.append(
            "OrbStack is installed: its built-in Kubernetes is a lighter alternative; this guide uses minikube."
        )
    report.show(report.panel("Ready", report.bullets(nexts), style="green"))


def run() -> bool:
    """Print the checks; True when no hard requirement failed."""
    host = collect()
    results = checks(host)
    render(host, results)
    return all(check.status is not Status.FAIL for check in results)
