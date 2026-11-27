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
class Observation:
    """One span or generation as Langfuse recorded it."""

    id: str
    trace_id: str
    parent_id: str | None
    type: str  # "SPAN", "GENERATION", "RETRIEVER", ...
    name: str
    start_time: datetime
    level: str  # "DEFAULT", or "ERROR" for a failed call
    latency: float  # seconds
    model: str | None
    status_message: str
    total_tokens: int
    total_cost: float
    prompt_name: str | None
    prompt_version: int | None
    metadata: dict[str, Any]

    @property
    def tags(self) -> list[str]:
        return list(self.metadata.get("attributes.langfuse.trace.tags", []))

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Observation":
        metadata: dict[str, Any] = data.get("metadata") or {}
        return cls(
            id=data["id"],
            trace_id=data["traceId"],
            parent_id=data.get("parentObservationId") or None,
            type=data["type"],
            name=data.get("name") or "",
            start_time=datetime.fromisoformat(data["startTime"]),
            level=data["level"],
            latency=float(data.get("latency") or 0),
            model=data.get("model") or None,
            status_message=data.get("statusMessage") or "",
            total_tokens=int(data.get("totalUsage") or 0),
            total_cost=float(data.get("totalCost") or 0),
            prompt_name=data.get("promptName") or None,
            prompt_version=data.get("promptVersion"),
            metadata=metadata,
        )


@dataclass(frozen=True)
class Attempt:
    """One request sent by the load generator."""

    started: float  # seconds since the run began
    latency: float
    ttft: float | None
    error: str | None
