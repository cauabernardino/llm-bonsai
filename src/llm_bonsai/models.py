"""Data shared across the package: what the lab is made of, and what comes back from it.

Plain records only (standard library, no I/O). A type used by a single probe lives in
that probe instead.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Service:
    namespace: str
    name: str
    port: int


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


@dataclass(frozen=True)
class Team:
    """A gateway team with one long-lived virtual key, saved to .env as `env_var`."""

    alias: str
    env_var: str
    models: list[str]
    rpm_limit: int
    max_budget: float

    def key_limits(self) -> dict[str, Any]:
        return {"models": self.models, "rpm_limit": self.rpm_limit, "max_budget": self.max_budget}


@dataclass(frozen=True)
class GatewayPod:
    name: str
    ready: bool
    terminating: bool

    @classmethod
    def from_api(cls, pod: dict[str, Any]) -> "GatewayPod":
        conditions = pod.get("status", {}).get("conditions", [])
        return cls(
            name=pod["metadata"]["name"],
            ready=any(c["type"] == "Ready" and c["status"] == "True" for c in conditions),
            terminating=bool(pod["metadata"].get("deletionTimestamp")),
        )


@dataclass(frozen=True)
class Generation:
    """One model call as Langfuse recorded it."""

    start_time: datetime
    level: str  # "DEFAULT", or "ERROR" for a failed call
    latency: float  # seconds
    model: str | None
    status_message: str
    total_tokens: int
    total_cost: float
    tags: list[str]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Generation":
        metadata = data.get("metadata") or {}
        return cls(
            start_time=datetime.fromisoformat(data["startTime"]),
            level=data["level"],
            latency=float(data.get("latency") or 0),
            model=data.get("model"),
            status_message=data.get("statusMessage") or "",
            total_tokens=int(data.get("totalUsage") or 0),
            total_cost=float(data.get("totalCost") or 0),
            tags=list(metadata.get("attributes.langfuse.trace.tags", [])),
        )


@dataclass(frozen=True)
class Attempt:
    """One request sent by the load generator."""

    started: float  # seconds since the run began
    latency: float
    ttft: float | None
    error: str | None
