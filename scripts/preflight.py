"""Collect host facts that decide how the lab cluster is created.

Prints a Markdown report (paste-ready for NOTES.md) and a cluster recommendation.
Read-only: it never installs or starts anything.
"""

import json
import platform
import shutil
import subprocess
from pathlib import Path

GIB = 1024**3
MIN_FREE_DISK_GIB = 40
CLUSTER_MEMORY_GIB = 14
# The Docker VM needs room for its own kernel, dockerd and image unpacking on top of
# minikube's limit; with only ~0.6 GiB spare the whole minikube container got OOM-killed.
# Docker Desktop set to 17 GB shows up as ~16.6 GiB of MemTotal inside the VM.
DOCKER_DESKTOP_MEMORY_GB = 17
MIN_DOCKER_VM_GIB = 16.5
TOOLS = {
    "kubectl": ["kubectl", "version", "--client", "-o", "json"],
    "helm": ["helm", "version", "--short"],
    "minikube": ["minikube", "version", "--short"],
    "orbctl": ["orbctl", "version"],
    "uv": ["uv", "--version"],
}


def run(cmd: list[str]) -> str | None:
    """Run a command and return stripped stdout, or None if it is missing or fails."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip()


def tool_version(name: str, cmd: list[str]) -> str | None:
    out = run(cmd)
    if out is None:
        return None
    if name == "kubectl":
        return json.loads(out)["clientVersion"]["gitVersion"]
    return out.splitlines()[0]


def docker_info() -> dict | None:
    out = run(["docker", "info", "--format", "{{json .}}"])
    if out is None:
        return None
    info = json.loads(out)
    # The CLI can return JSON with ServerErrors when the daemon is down.
    if info.get("ServerErrors") or not info.get("MemTotal"):
        return None
    return info


def kube_contexts() -> tuple[list[str], str | None]:
    names = run(["kubectl", "config", "get-contexts", "-o", "name"])
    current = run(["kubectl", "config", "current-context"])
    return (names.splitlines() if names else []), current


def main() -> None:
    ram_gib = int(run(["sysctl", "-n", "hw.memsize"]) or 0) / GIB
    chip = run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "unknown"
    free_disk_gib = shutil.disk_usage(Path.home()).free / GIB
    docker = docker_info()
    tools = {name: tool_version(name, cmd) for name, cmd in TOOLS.items()}
    contexts, current_context = kube_contexts()

    # M1 lacks the bf16 instructions vLLM's CPU backend wants; M2+ has them.
    is_m1 = "M1" in chip
    docker_mem_gib = docker["MemTotal"] / GIB if docker else None

    print("## Preflight results\n")
    print(f"- macOS {platform.mac_ver()[0]}, arch {platform.machine()}")
    print(f"- Chip: {chip} → vLLM dtype: {'float16' if is_m1 else 'bfloat16'}")
    print(f"- RAM: {ram_gib:.0f} GiB")
    disk_flag = "OK" if free_disk_gib >= MIN_FREE_DISK_GIB else f"LOW (need ≥ {MIN_FREE_DISK_GIB})"
    print(f"- Free disk: {free_disk_gib:.0f} GiB ({disk_flag})")
    if docker:
        print(
            f"- Docker: {docker.get('OperatingSystem')} {docker.get('ServerVersion')}, "
            f"VM {docker_mem_gib:.1f} GiB RAM / {docker.get('NCPU')} CPUs"
        )
    elif shutil.which("docker"):
        print("- Docker: CLI installed, daemon not running (start Docker Desktop)")
    else:
        print("- Docker: **missing**")

    print("\n| Tool | Version |\n|---|---|")
    for name, version in tools.items():
        print(f"| {name} | {version or '**missing**'} |")

    print(f"\n- Kube contexts: {', '.join(contexts) or 'none'}")
    print(f"- Current context: {current_context or 'none'}")

    print("\n## Recommendation\n")
    if tools["orbctl"]:
        print(f"- Cluster: OrbStack built-in Kubernetes (set its memory limit to ~{CLUSTER_MEMORY_GIB} GiB).")
    else:
        print(
            f"- Cluster: minikube --driver=docker --cpus=6 --memory={CLUSTER_MEMORY_GIB}g --disk-size=40g"
        )
        if docker_mem_gib is not None and docker_mem_gib < MIN_DOCKER_VM_GIB:
            print(
                f"- Docker VM has {docker_mem_gib:.1f} GiB; set Docker Desktop → Settings → Resources "
                f"→ Memory to ≥ {DOCKER_DESKTOP_MEMORY_GB} GB before starting minikube."
            )
    missing = [name for name, version in tools.items() if version is None and name != "orbctl"]
    if tools["orbctl"]:
        missing = [name for name in missing if name != "minikube"]
    if missing:
        print(f"- Install with Homebrew (after confirmation): brew install {' '.join(missing)}")


if __name__ == "__main__":
    main()
