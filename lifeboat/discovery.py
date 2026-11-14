"""Find tiered workloads (label lifeboat.io/tier) and cluster capacity."""
from __future__ import annotations

import logging

from kubernetes import client

from .checkpoint import CheckpointStore
from .config import Config
from .models import PlanRecord, Tier, Workload, step_key

TIER_LABEL = "lifeboat.io/tier"
RESTART_ANNOTATION = "lifeboat.io/restart-seconds"
OVERDUE_ANNOTATION = "lifeboat.io/overdue"

log = logging.getLogger("lifeboat.discovery")


def parse_cpu(q: str) -> int:
    q = str(q)
    return int(float(q[:-1])) if q.endswith("m") else int(float(q) * 1000)


def _pod_millis(template) -> int:
    return sum(parse_cpu((c.resources.requests or {}).get("cpu", "0"))
               for c in template.spec.containers if c.resources)


def job_finished(job) -> bool:
    """A finished Job frees nothing when paused, so it must not count as capacity."""
    st = job.status
    if st is None:
        return False
    if getattr(st, "completion_time", None):
        return True
    return any(c.type in ("Complete", "Failed") and c.status == "True" for c in (st.conditions or []))


def _tier(obj):
    """Tier from the label, or None (with a warning) for a mistyped label: one bad workload
    must not crash every tick."""
    label = (obj.metadata.labels or {}).get(TIER_LABEL, "")
    try:
        return Tier.from_label(label)
    except (KeyError, ValueError):
        log.warning("%s/%s has unknown tier label %r, ignoring it", obj.metadata.namespace,
                    obj.metadata.name, label)
        return None


def _latest_checkpoint(cp: CheckpointStore, name: str):
    """The checkpoint store is not allowed to stall or break a tick: failure => none known."""
    try:
        return cp.latest_ts(name)
    except Exception as e:
        log.warning("checkpoint lookup for %s failed: %s", name, e)
        return None


def list_workloads(cfg: Config, cp: CheckpointStore) -> list[Workload]:
    batch, apps = client.BatchV1Api(), client.AppsV1Api()
    ns, out = cfg.workload_namespace, []
    for j in batch.list_namespaced_job(ns, label_selector=TIER_LABEL).items:
        tier = _tier(j)
        if tier is None or job_finished(j):
            continue
        a = j.metadata.annotations or {}
        out.append(Workload(
            j.metadata.name, ns, tier, "Job",
            _pod_millis(j.spec.template), j.spec.parallelism or 1, _latest_checkpoint(cp, j.metadata.name),
            float(a.get(RESTART_ANNOTATION, 30)), a.get(OVERDUE_ANNOTATION, "false") == "true"))
    for d in apps.list_namespaced_deployment(ns, label_selector=TIER_LABEL).items:
        tier = _tier(d)
        if tier is None:
            continue
        out.append(Workload(
            d.metadata.name, ns, tier, "Deployment",
            _pod_millis(d.spec.template), d.spec.replicas or 0))
    return out


def apply_originals(workloads: list[Workload], rec: PlanRecord | None) -> None:
    """Use pre-triage replica counts so the planner sees the world as it was before we acted."""
    if not rec:
        return
    for w in workloads:
        if w.kind == "Deployment":
            st = rec.steps.get(step_key("scale_deployment", w.namespace, w.name))
            if st and st.original.get("replicas") is not None:
                w.replicas = st.original["replicas"]


def free_millis() -> float:
    core = client.CoreV1Api()
    alloc = sum(parse_cpu(n.status.allocatable["cpu"]) for n in core.list_node().items)
    used = 0
    for p in core.list_pod_for_all_namespaces().items:
        if p.spec.node_name and p.status.phase in ("Pending", "Running"):
            used += sum(parse_cpu((c.resources.requests or {}).get("cpu", "0"))
                        for c in p.spec.containers if c.resources)
    return float(alloc - used)


def is_unschedulable(pod) -> bool:
    """Stuck waiting for room: PodScheduled=False/Unschedulable. Plain phase=Pending would also
    count pods that are only pulling images or starting, so every C1 scale-up by Lifeboat
    itself would look like C1 struggling and trip L3."""
    for c in (pod.status.conditions or []) if pod.status else []:
        if c.type == "PodScheduled" and c.status == "False" and c.reason == "Unschedulable":
            return True
    return False


def c1_unschedulable_pods(cfg: Config) -> int:
    core = client.CoreV1Api()
    pods = core.list_namespaced_pod(cfg.workload_namespace, label_selector=f"{TIER_LABEL}=C1",
                                    field_selector="status.phase=Pending").items
    return sum(1 for p in pods if is_unschedulable(p))
