"""Find tiered workloads (label lifeboat.io/tier) and cluster capacity."""
from __future__ import annotations

from kubernetes import client

from .checkpoint import CheckpointStore
from .config import Config
from .models import PlanRecord, Tier, Workload, step_key

TIER_LABEL = "lifeboat.io/tier"
RESTART_ANNOTATION = "lifeboat.io/restart-seconds"
OVERDUE_ANNOTATION = "lifeboat.io/overdue"


def parse_cpu(q: str) -> int:
    q = str(q)
    return int(float(q[:-1])) if q.endswith("m") else int(float(q) * 1000)


def _pod_millis(template) -> int:
    return sum(parse_cpu((c.resources.requests or {}).get("cpu", "0"))
               for c in template.spec.containers if c.resources)


def list_workloads(cfg: Config, cp: CheckpointStore) -> list[Workload]:
    batch, apps = client.BatchV1Api(), client.AppsV1Api()
    ns, out = cfg.workload_namespace, []
    for j in batch.list_namespaced_job(ns, label_selector=TIER_LABEL).items:
        a = j.metadata.annotations or {}
        out.append(Workload(
            j.metadata.name, ns, Tier.from_label(j.metadata.labels[TIER_LABEL]), "Job",
            _pod_millis(j.spec.template), j.spec.parallelism or 1, cp.latest_ts(j.metadata.name),
            float(a.get(RESTART_ANNOTATION, 30)), a.get(OVERDUE_ANNOTATION, "false") == "true"))
    for d in apps.list_namespaced_deployment(ns, label_selector=TIER_LABEL).items:
        out.append(Workload(
            d.metadata.name, ns, Tier.from_label(d.metadata.labels[TIER_LABEL]), "Deployment",
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


def c1_pending_pods(cfg: Config) -> int:
    core = client.CoreV1Api()
    pods = core.list_namespaced_pod(cfg.workload_namespace, label_selector=f"{TIER_LABEL}=C1",
                                    field_selector="status.phase=Pending").items
    return len(pods)
