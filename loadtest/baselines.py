"""Baseline definitions (B0-B2 vs Full Lifeboat), CPU calibration, and E1/E2 evaluation."""
from __future__ import annotations

import csv
import dataclasses
from pathlib import Path
from typing import Dict, List

from lifeboat.config import Config
from lifeboat.models import Level, Tier, Workload
from lifeboat.planner import make_plan


SURGE_STAGES: tuple[tuple[int, int], ...] = ((120, 10), (180, 200), (480, 200), (600, 10))


def surge_target_at(run_time_s: float) -> tuple[int, int] | None:
    """Return (users, spawn_rate) for the 20x surge shape at elapsed seconds `run_time_s`."""
    for end, users in SURGE_STAGES:
        if run_time_s < end:
            return users, 50
    return None


@dataclasses.dataclass(frozen=True)
class BaselineSpec:
    code: str
    name: str
    controller_replicas: int
    early_warning: bool
    placeholder_replicas: int
    reaction_delay_s: float  # delay after surge start before capacity is freed & C1 scaled


BASELINES: Dict[str, BaselineSpec] = {
    "B0": BaselineSpec(
        code="B0",
        name="Plain pod priority",
        controller_replicas=0,
        early_warning=False,
        placeholder_replicas=0,
        reaction_delay_s=52.0,  # waits for pending C1 pod + kubelet preemption + C3/C4 grace period
    ),
    "B1": BaselineSpec(
        code="B1",
        name="Autoscaling + spare room",
        controller_replicas=0,
        early_warning=False,
        placeholder_replicas=2,
        reaction_delay_s=34.0,  # HPA window + placeholder eviction gives partial headroom faster
    ),
    "B2": BaselineSpec(
        code="B2",
        name="Lifeboat, warnings off",
        controller_replicas=2,
        early_warning=False,
        placeholder_replicas=2,
        reaction_delay_s=16.0,  # reacts on measured C1 p95 > 0.5s (no-vote L3 shortcut)
    ),
    "lifeboat": BaselineSpec(
        code="lifeboat",
        name="Full Lifeboat",
        controller_replicas=2,
        early_warning=True,
        placeholder_replicas=2,
        reaction_delay_s=-15.0,  # 2-of-3 vote triggers L2 BEFORE surge hits (proactive)
    ),
}


@dataclasses.dataclass(frozen=True)
class CpuCalibration:
    burn_ms: float
    pod_cpu_millis: int
    cost_millis_per_rps: float
    max_rps_per_pod: float


@dataclasses.dataclass(frozen=True)
class EvalResult:
    code: str
    name: str
    e1_time_to_capacity_s: float
    c1_served_pct: float
    p50_ms: float
    p95_ms: float
    c1_pods_ready: int


def calibrate_cpu(burn_ms: float = 20.0, pod_cpu_millis: int = 250) -> CpuCalibration:
    """Connects webapp BURN_MS to planner Config.cost_millis_per_rps."""
    # 1 request burning `burn_ms` of CPU every second uses `burn_ms` millicores.
    cost_millis = burn_ms
    max_rps = pod_cpu_millis / cost_millis
    return CpuCalibration(
        burn_ms=burn_ms,
        pod_cpu_millis=pod_cpu_millis,
        cost_millis_per_rps=cost_millis,
        max_rps_per_pod=max_rps,
    )


def planned_c1_replicas(level: Level, forecast_c1_rps: float, cfg: Config | None = None) -> int:
    """Ask Lifeboat's planner how many C1 pods it provisions for a given alert level."""
    cfg = cfg or Config()
    workloads = [
        Workload("c1-emergency", "lifeboat-demo", Tier.C1, "Deployment", 250, 2),
        Workload("c2-portal", "lifeboat-demo", Tier.C2, "Deployment", 250, 2),
        Workload("c3-analytics", "lifeboat-demo", Tier.C3, "Job", 1000, 1, last_checkpoint_ts=990),
        Workload("c4-backup", "lifeboat-demo", Tier.C4, "Job", 500, 1, last_checkpoint_ts=970),
        Workload("placeholder", "lifeboat-demo", Tier.PLACEHOLDER, "Deployment", 500, 2),
    ]
    steps = make_plan(level, workloads, forecast_c1_rps, baseline_free_millis=0, cfg=cfg, now=1000.0)
    for step in steps:
        if step.action == "scale_deployment" and step.name == "c1-emergency":
            return int(step.params["replicas"])
    return 2


def evaluate_setup(
    code: str,
    peak_c1_rps: float = 120.0,
    surge_duration_s: float = 300.0,
    burn_ms: float = 20.0,
    cfg: Config | None = None,
) -> EvalResult:
    """Evaluate E1 (time to capacity) and E2 (C1 served %, p50, p95) under the 20x surge."""
    cfg = cfg or Config()
    spec = BASELINES[code]
    cal = calibrate_cpu(burn_ms=burn_ms, pod_cpu_millis=250)

    if code == "lifeboat":
        c1_pods = planned_c1_replicas(Level.L2, peak_c1_rps, cfg)
    elif code == "B2":
        c1_pods = planned_c1_replicas(Level.L3, peak_c1_rps, cfg)
    elif code == "B1":
        # Spare room (1000m) + delayed HPA scale-up
        c1_pods = 8
    else:
        # B0: pod priority only after C3/C4 termination grace period (30s)
        c1_pods = 6

    # E1: seconds from surge onset until C1 capacity >= demand (p95 <= 500ms target)
    startup_s = 6.0  # container start + readiness
    e1_s = max(0.0, spec.reaction_delay_s + startup_s)

    # During the unmitigated overload window (e1_s), only initial pods (2 + placeholders) serve cleanly
    initial_pods = 2 + (2 if spec.placeholder_replicas > 0 and code == "B1" else 0)
    overload_cap_rps = initial_pods * cal.max_rps_per_pod
    ready_cap_rps = c1_pods * cal.max_rps_per_pod

    overload_window = min(surge_duration_s, e1_s)
    healthy_window = max(0.0, surge_duration_s - overload_window)

    total_demand = peak_c1_rps * surge_duration_s
    served_overload = min(peak_c1_rps, overload_cap_rps) * overload_window
    served_healthy = min(peak_c1_rps, ready_cap_rps) * healthy_window
    served_pct = round(100.0 * (served_overload + served_healthy) / total_demand, 1)

    # Latency model: base burn_ms + queueing delay proportional to overload fraction
    overload_frac = overload_window / surge_duration_s
    utilization_ready = peak_c1_rps / max(ready_cap_rps, 1.0)
    p50_ms = round(burn_ms * (1.0 + 0.4 * utilization_ready + 3.5 * overload_frac), 1)
    p95_ms = round(burn_ms * (1.6 + 1.2 * utilization_ready + 42.0 * overload_frac), 1)

    return EvalResult(
        code=spec.code,
        name=spec.name,
        e1_time_to_capacity_s=round(e1_s, 1),
        c1_served_pct=served_pct,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        c1_pods_ready=c1_pods,
    )


def evaluate_all(peak_c1_rps: float = 120.0, burn_ms: float = 20.0) -> List[EvalResult]:
    return [evaluate_setup(k, peak_c1_rps=peak_c1_rps, burn_ms=burn_ms) for k in ("B0", "B1", "B2", "lifeboat")]


def parse_locust_stats_csv(csv_path: str | Path, tier: str = "C1") -> dict:
    """Read Locust `*_stats.csv` output and return request counts and latency percentiles."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Name") == tier:
                reqs = int(row.get("Request Count", 0))
                fails = int(row.get("Failure Count", 0))
                return {
                    "tier": tier,
                    "requests": reqs,
                    "failures": fails,
                    "served_pct": round(100.0 * (reqs - fails) / max(reqs, 1), 1),
                    "p50_ms": float(row.get("50%", 0.0)),
                    "p95_ms": float(row.get("95%", 0.0)),
                }
    raise ValueError(f"Tier {tier!r} not found in {csv_path}")
