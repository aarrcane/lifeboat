"""Algorithm 2 - planning (greedy). Pure logic.

Inputs must be *pre-triage* views of the world, otherwise the plan would flip-flop
as our own actions free room:
  * workloads carry ORIGINAL replicas (controller substitutes them from the TriagePlan)
  * baseline_free_millis = free_now - sum(delta_millis of steps already done)
"""
from __future__ import annotations

import math
import time
from typing import Optional

from .config import Config
from .models import Level, Step, Tier, Workload, step_key


def _score(w: Workload, now: float) -> float:
    """Room freed per unit of progress lost. Higher = pause first."""
    lost = (now - w.last_checkpoint_ts) if w.last_checkpoint_ts else 3600.0
    return w.millis / (max(lost, 0.0) + w.restart_seconds + 1.0)


def _pause(w: Workload) -> Step:
    if w.kind == "Job":
        return Step(step_key("suspend_job", w.namespace, w.name), "suspend_job",
                    w.namespace, w.name, {"delta_millis": w.millis, "checkpoint": True})
    return Step(step_key("scale_deployment", w.namespace, w.name), "scale_deployment",
                w.namespace, w.name,
                {"replicas": 0, "delta_millis": w.millis, "checkpoint": True})


def make_plan(level: Level, workloads: list[Workload], c1_rps_forecast: float,
              baseline_free_millis: float, cfg: Config,
              now: Optional[float] = None) -> list[Step]:
    level = Level(level)
    now = now if now is not None else time.time()
    if level == Level.L0:
        return []

    by = lambda t: [w for w in workloads if w.tier == t]  # noqa: E731
    c1, c3, c4, ph = by(Tier.C1), by(Tier.C3), by(Tier.C4), by(Tier.PLACEHOLDER)
    steps: list[Step] = []

    # ---- C1 targets (sizing from forecast at L2+, a few early pods at L1)
    c1_total = sum(w.millis for w in c1)
    needed = c1_rps_forecast * cfg.cost_millis_per_rps + cfg.margin_millis
    extra_pods = {Level.L1: cfg.l1_extra_c1_pods, Level.L2: cfg.l1_extra_c1_pods,
                  Level.L3: cfg.l3_extra_c1_pods}[level]
    c1_scale: list[Step] = []
    for w in c1:
        if w.kind != "Deployment":
            continue
        target = w.replicas + extra_pods
        if level >= Level.L2 and c1_total > 0:
            share = needed * (w.millis / c1_total)
            target = max(target, math.ceil(share / max(w.pod_millis, 1)))
        target = min(target, cfg.c1_max_replicas)
        if target > w.replicas:
            c1_scale.append(Step(step_key("scale_deployment", w.namespace, w.name),
                                 "scale_deployment", w.namespace, w.name,
                                 {"replicas": target,
                                  "delta_millis": -(target - w.replicas) * w.pod_millis}))
    extra_millis = -sum(s.params["delta_millis"] for s in c1_scale)

    # ---- pauses
    ordered = lambda ws: sorted(ws, key=lambda w: _score(w, now), reverse=True)  # noqa: E731
    base = ordered([w for w in c4 if not w.overdue])        # L1: cheap C4 pauses
    pauses = [_pause(w) for w in base]
    freed = sum(w.millis for w in base)

    placeholder_steps: list[Step] = []
    config_steps: list[Step] = []
    if level >= Level.L2:
        for p in ph:
            placeholder_steps.append(Step(
                step_key("scale_deployment", p.namespace, p.name), "scale_deployment",
                p.namespace, p.name, {"replicas": 0, "delta_millis": p.millis}))
        freed += sum(p.millis for p in ph)
        shortfall = extra_millis - baseline_free_millis - freed
        candidates = ordered([w for w in c4 if w.overdue]) + ordered(c3)   # C4 first, then C3
        for w in candidates:
            if level < Level.L3 and shortfall <= 0:
                break                                        # L3 pauses everything pausable
            pauses.append(_pause(w))
            shortfall -= w.millis
    if level >= Level.L3:
        config_steps.append(Step(
            step_key("set_configmap", cfg.workload_namespace, cfg.c2_configmap),
            "set_configmap", cfg.workload_namespace, cfg.c2_configmap,
            {"data": {"degraded": "true"}, "delta_millis": 0}))

    steps = pauses + placeholder_steps + c1_scale + config_steps
    return steps
