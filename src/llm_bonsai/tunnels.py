"""Port-forwards that live exactly as long as the command that needs them.

A command that talks to the gateway or Langfuse forwards a free local port to that
Service with `kubectl port-forward`, and closes it when the command exits, so no port
stays open between runs. `kubectl port-forward svc/...` pins one pod and exits when that
pod goes away, so a watcher thread restarts it; `restart()` covers the case where it
stays attached to a pod that is shutting down and never answers.
"""

import atexit
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from llm_bonsai import kube
from llm_bonsai.errors import LabError

RESTART_DELAY_S = 0.5


@dataclass(frozen=True)
class Service:
    namespace: str
    name: str
    port: int


GATEWAY = Service("llm", "litellm", 4000)
LANGFUSE = Service("observability", "langfuse-web", 3000)


def free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


class PortForward:
    """`kubectl port-forward` to one Service, restarted whenever it exits, until closed."""

    def __init__(self, service: Service, local_port: int | None = None) -> None:
        self.service = service
        self.local_port = local_port or free_local_port()
        self._process: subprocess.Popen[bytes] | None = None
        self._last_error = ""
        self._closed = threading.Event()
        self._lock = threading.Lock()  # so close() can't race the watcher starting a process
        self._watcher = threading.Thread(target=self._keep_forwarding, daemon=True)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.local_port}"

    def open(self, timeout_s: float = 20) -> None:
        self._watcher.start()
        self._wait_until_listening(timeout_s)

    def restart(self, timeout_s: float = 20) -> None:
        """Drop the current connection; the watcher starts a fresh one, which picks a live pod."""
        self._stop_process()
        self._wait_until_listening(timeout_s)

    def close(self) -> None:
        with self._lock:
            self._closed.set()
        self._stop_process()
        self._watcher.join(timeout=5)

    def _stop_process(self) -> None:
        with self._lock:
            process = self._process
        if process:
            process.terminate()
            process.wait()

    def _keep_forwarding(self) -> None:
        target = f"svc/{self.service.name}"
        ports = f"{self.local_port}:{self.service.port}"
        while not self._closed.is_set():
            with tempfile.TemporaryFile() as stderr:
                with self._lock:
                    if self._closed.is_set():
                        return
                    self._process = subprocess.Popen(
                        [*kube.kubectl_cmd(namespace=self.service.namespace), "port-forward", target, ports],
                        stdout=subprocess.DEVNULL,
                        stderr=stderr,
                    )
                self._process.wait()
                stderr.seek(0)
                lines = stderr.read().decode(errors="replace").strip().splitlines()
                if lines:
                    self._last_error = lines[-1]
            self._closed.wait(RESTART_DELAY_S)

    def _wait_until_listening(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.2)
        service = f"svc/{self.service.name} in namespace {self.service.namespace}"
        raise LabError(
            f"could not port-forward {service}: {self._last_error or 'no answer'}. "
            "Is the lab installed and the cluster running? (`llm-bonsai setup all`)"
        )


_open: dict[Service, PortForward] = {}


def forward(service: Service) -> PortForward:
    """The port-forward to `service`, opened on first use and closed when the process exits."""
    if service not in _open:
        tunnel = PortForward(service)
        tunnel.open()
        if not _open:
            atexit.register(close_all)
        _open[service] = tunnel
    return _open[service]


def url(service: Service) -> str:
    return forward(service).url


def close_all() -> None:
    while _open:
        _open.popitem()[1].close()


def hold_open_for_browser() -> None:
    """Forward Langfuse to localhost:3000 and LiteLLM to localhost:4000 until Ctrl-C.

    Fixed ports, unlike the commands' own tunnels: Langfuse's login only works on the
    URL it was configured with (NEXTAUTH_URL=http://localhost:3000).
    """
    pages = [(LANGFUSE, 3000), (GATEWAY, 4000)]
    busy = [port for _, port in pages if port_in_use(port)]
    if busy:
        raise LabError(f"port {' and '.join(map(str, busy))} already in use (an old `kubectl port-forward`?)")
    tunnels = [PortForward(service, port) for service, port in pages]
    try:
        for tunnel in tunnels:
            tunnel.open()
        print("Langfuse:   http://localhost:3000     log in with LANGFUSE_INIT_USER_EMAIL / _PASSWORD from .env")
        print("LiteLLM UI: http://localhost:4000/ui  log in as admin with LITELLM_MASTER_KEY from .env")
        print("Ctrl-C closes both.", flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nclosed")
    finally:
        for tunnel in tunnels:
            tunnel.close()
