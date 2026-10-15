from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional


class Level(IntEnum):
    L0 = 0  # NORMAL
    L1 = 1  # WATCH
    L2 = 2  # EMERGENCY
    L3 = 3  # CRITICAL


class Tier(IntEnum):
    PLACEHOLDER = 0  # overprovisioning pods (baseline B1 / L0-L1 headroom)
    C1 = 1           # emergency services: never paused
    C2 = 2           # citizen portal: only loses extra features at L3
    C3 = 3           # analytics: paused first
    C4 = 4           # backups: paused first

    @classmethod
    def from_label(cls, label: str) -> "Tier":
        label = label.strip()
        return cls.PLACEHOLDER if label.lower() == "placeholder" else cls[label.upper()]


def step_key(action: str, namespace: str, name: str) -> str:
    return f"{action}:{namespace}/{name}"


@dataclass(frozen=True)
class Reading:
    source: str          # "S1" PUB | "S2" NEA | "S3" forecast
    level: int
    ts: float
    expires_at: float
    sig: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Reading":
        return cls(str(d["source"]), int(d["level"]), float(d["ts"]),
                   float(d["expires_at"]), str(d.get("sig", "")))


@dataclass
class Workload:
    name: str
    namespace: str
    tier: Tier
    kind: str                        # "Job" | "Deployment"
    pod_millis: int                  # CPU request per pod
    replicas: int = 1                # Jobs: parallelism. Deployments: ORIGINAL replicas
    last_checkpoint_ts: Optional[float] = None
    restart_seconds: float = 30.0
    overdue: bool = False            # an overdue backup keeps running at L1

    @property
    def millis(self) -> int:
        return self.pod_millis * self.replicas


@dataclass
class Step:
    key: str
    action: str                      # suspend_job | scale_deployment | set_configmap
    namespace: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    original: dict[str, Any] = field(default_factory=dict)
    done: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(**d)


@dataclass
class PlanRecord:
    """The TriagePlan. Persisted in etcd as a custom resource."""
    level: int = 0
    takeover: int = 0
    steps: dict[str, Step] = field(default_factory=dict)   # insertion-ordered

    def to_spec(self) -> dict:
        return {"level": self.level, "takeover": self.takeover,
                "steps": [s.to_dict() for s in self.steps.values()]}

    @classmethod
    def from_spec(cls, spec: dict) -> "PlanRecord":
        steps = {}
        for d in spec.get("steps", []):
            s = Step.from_dict(d)
            steps[s.key] = s
        return cls(int(spec.get("level", 0)), int(spec.get("takeover", 0)), steps)
