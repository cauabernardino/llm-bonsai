"""The host requirements, on hosts made up for each case."""

from dataclasses import replace

import pytest

from llm_bonsai.setup.preflight import (
    Check,
    Docker,
    Host,
    Status,
    checks,
    parse_cpuinfo,
    parse_docker_info,
    parse_version,
)

LINUX_DOCKER = Docker(memory_gib=31.0, cpus=16, version="29.9.3", in_vm=False, root_dir="/var/lib/docker")
LINUX = Host(
    system="Linux", wsl=False, os_name="Ubuntu 24.04 LTS", arch="x86_64", cpu="AMD Ryzen 9", bf16=True, ram_gib=31.0,
    docker=LINUX_DOCKER, docker_cli=True, free_disk_gib=200.0, disk_path="/var/lib/docker",
    tools={"kubectl": "v1.37.3", "helm": "v4.3.2+g1a2b3c", "minikube": "minikube version: v1.39.0"},
    lab_context="llm-bonsai", lab_context_exists=False, orbstack=False,
)  # fmt: skip
MAC_DOCKER = replace(LINUX_DOCKER, memory_gib=16.6, cpus=14, in_vm=True)
MAC = replace(
    LINUX, system="Darwin", os_name="macOS 26.7", arch="arm64", ram_gib=24.0, docker=MAC_DOCKER,
    disk_path="/Users/someone",
)  # fmt: skip


def by_name(found: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in found}


@pytest.mark.parametrize("host", [LINUX, MAC], ids=["linux", "macos"])
def test_a_host_meeting_every_requirement_passes(host: Host) -> None:
    assert all(check.status is Status.OK for check in checks(host))


def test_host_headroom_is_what_docker_desktop_or_the_cluster_leaves() -> None:
    assert by_name(checks(replace(MAC, ram_gib=18.0)))["RAM left for the host"].status is Status.WARN
    # Native Docker shares the host's RAM with the 14 GiB cluster.
    native = by_name(checks(replace(LINUX, ram_gib=16.6, docker=replace(LINUX_DOCKER, memory_gib=16.6))))
    assert native["Memory for Docker"].status is Status.OK
    assert native["RAM left for the host"].status is Status.WARN


def test_wsl_skips_the_headroom_check_and_points_at_wslconfig() -> None:
    wsl = replace(LINUX, wsl=True, ram_gib=15.0, docker=replace(LINUX_DOCKER, memory_gib=15.0, in_vm=True))
    found = by_name(checks(wsl))
    assert "RAM left for the host" not in found
    assert ".wslconfig" in (found["Memory for Docker"].fix or "")


def test_cpu_fix_depends_on_the_setup() -> None:
    few = replace(LINUX_DOCKER, cpus=4)
    assert "Docker Desktop" in (
        by_name(checks(replace(MAC, docker=replace(MAC_DOCKER, cpus=4))))["CPUs for Docker"].fix or ""
    )
    assert "more CPUs" in (by_name(checks(replace(LINUX, docker=few)))["CPUs for Docker"].fix or "")


def test_too_little_docker_memory_fails_with_a_fix_for_that_setup() -> None:
    in_vm = by_name(checks(replace(MAC, docker=replace(MAC_DOCKER, memory_gib=14.6))))
    assert in_vm["Memory for Docker"].status is Status.FAIL
    assert "Docker Desktop" in (in_vm["Memory for Docker"].fix or "")
    native = by_name(checks(replace(LINUX, docker=replace(LINUX_DOCKER, memory_gib=15.0))))
    assert "more RAM" in (native["Memory for Docker"].fix or "")


def test_a_stopped_docker_daemon_on_linux_points_at_systemctl_and_the_docker_group() -> None:
    docker = by_name(checks(replace(LINUX, docker=None)))["Docker"]
    assert docker.status is Status.FAIL
    assert "systemctl" in (docker.fix or "") and "usermod" in (docker.fix or "")


def test_missing_and_outdated_tools_fail_with_an_install_hint() -> None:
    host = replace(LINUX, tools={"kubectl": "v1.31.4", "helm": None, "minikube": "minikube version: v1.39.0"})
    found = by_name(checks(host))
    assert found["kubectl"].status is Status.FAIL and "kubernetes.io" in (found["kubectl"].fix or "")
    assert found["helm"].found == "missing" and "helm.sh" in (found["helm"].fix or "")
    assert found["minikube"].status is Status.OK
    mac = by_name(checks(replace(MAC, tools={**MAC.tools, "kubectl": "v1.31.4"})))
    assert mac["kubectl"].fix == "brew upgrade kubernetes-cli"


def test_unsupported_os_and_architecture_fail_without_crashing() -> None:
    windows = replace(LINUX, system="Windows", os_name="Windows 11", arch="ppc64le", ram_gib=None, docker=None)
    found = by_name(checks(windows))
    assert found["Operating system"].status is Status.FAIL and "WSL2" in (found["Operating system"].fix or "")
    assert found["CPU architecture"].status is Status.FAIL


def test_low_disk_fails() -> None:
    assert by_name(checks(replace(LINUX, free_disk_gib=12.0)))["Free disk"].status is Status.FAIL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("v1.37.3", (1, 37)), ("v4.3.2+g1a2b3c", (4, 3)), ("minikube version: v1.39.0", (1, 39)), ("garbage", None)],
)
def test_parse_version(raw: str, expected: tuple[int, int] | None) -> None:
    assert parse_version(raw) == expected


X86_CPUINFO = """processor\t: 0
model name\t: AMD EPYC 9654 96-Core Processor
flags\t\t: fpu vme avx512f avx512_bf16 avx512_vnni
"""
ARM64_CPUINFO = """processor\t: 0
BogoMIPS\t: 2000.00
Features\t: fp asimd evtstrm aes sha2 crc32 atomics bf16 i8mm
CPU implementer\t: 0x41
"""


@pytest.mark.parametrize(
    ("cpuinfo", "expected"),
    [
        (X86_CPUINFO, ("AMD EPYC 9654 96-Core Processor", True)),
        (ARM64_CPUINFO, (None, True)),
        (X86_CPUINFO.replace(" avx512_bf16", ""), ("AMD EPYC 9654 96-Core Processor", False)),
        ("processor\t: 0\n", (None, None)),
    ],
    ids=["x86-bf16", "arm64-no-model-name", "x86-no-bf16", "no-flags"],
)
def test_parse_cpuinfo(cpuinfo: str, expected: tuple[str | None, bool | None]) -> None:
    assert parse_cpuinfo(cpuinfo) == expected


def test_docker_info_tells_docker_desktop_from_native_engine() -> None:
    info = {"MemTotal": 17812598784, "NCPU": 14, "ServerVersion": "29.9.3", "DockerRootDir": "/var/lib/docker"}
    native = parse_docker_info({**info, "OperatingSystem": "Ubuntu 24.04 LTS"}, "Linux")
    desktop = parse_docker_info({**info, "OperatingSystem": "Docker Desktop"}, "Linux")
    assert native is not None and not native.in_vm and round(native.memory_gib, 1) == 16.6
    assert desktop is not None and desktop.in_vm
    assert parse_docker_info({"ServerErrors": ["Cannot connect to the Docker daemon"]}, "Linux") is None
