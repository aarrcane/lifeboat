"""Find tiered workloads (label lifeboat.io/tier) and cluster capacity."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

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


def _latest_checkpoint(cp: CheckpointStore, name: str, cache_s: float = 0.0, now: Optional[float] = None):
    """The checkpoint store is not allowed to stall or break a tick: failure => none known.
    Successful lookups are cached for `cache_s` seconds, so a slow S3 costs at most one lookup
    per workload per `cache_s` instead of one every tick (each can take seconds when S3 hangs)."""
    now = time.time() if now is None else now
    cache = getattr(cp, "_lifeboat_ts_cache", None)
    if cache is None:
        cache = {}
        try:
            setattr(cp, "_lifeboat_ts_cache", cache)
        except AttributeError:
            pass
    hit = cache.get(name)
    if cache_s > 0 and hit and now - hit[0] < cache_s:
        return hit[1]
    try:
        ts = cp.latest_ts(name)
    except Exception as e:
        log.warning("checkpoint lookup for %s failed: %s", name, e)
        return hit[1] if hit else None
    cache[name] = (now, ts)
    return ts


def _restart_seconds(obj, default: float = 30.0) -> float:
    """lifeboat.io/restart-seconds, or the default (with a warning) for a value like "20s"."""
    raw = (obj.metadata.annotations or {}).get(RESTART_ANNOTATION)
    if raw is None:
        return default
    try:
        v = float(raw)
        if v < 0 or v != v:
            raise ValueError(raw)
        return v
    except (TypeError, ValueError):
        log.warning("%s/%s has bad %s=%r, using %.0f", obj.metadata.namespace, obj.metadata.name,
                    RESTART_ANNOTATION, raw, default)
        return default


def list_workloads(cfg: Config, cp: CheckpointStore) -> list[Workload]:
    """Every tiered Job and Deployment. One object that cannot be read (bad annotation, odd CPU
    quantity) is skipped with a warning: it must never stop the tick for all the others."""
    batch, apps = client.BatchV1Api(), client.AppsV1Api()
    ns, out = cfg.workload_namespace, []
    to = cfg.api_timeout_s
    for j in batch.list_namespaced_job(ns, label_selector=TIER_LABEL, _request_timeout=to).items:
        try:
            tier = _tier(j)
            if tier is None or job_finished(j):
                continue
            a = j.metadata.annotations or {}
            out.append(Workload(
                j.metadata.name, ns, tier, "Job",
                _pod_millis(j.spec.template), j.spec.parallelism or 1,
                _latest_checkpoint(cp, j.metadata.name, cfg.checkpoint_cache_s),
                _restart_seconds(j), a.get(OVERDUE_ANNOTATION, "false") == "true"))
        except Exception as e:
            log.warning("skipping job %s: %s", getattr(j.metadata, "name", "?"), e)
    for d in apps.list_namespaced_deployment(ns, label_selector=TIER_LABEL, _request_timeout=to).items:
        try:
            tier = _tier(d)
            if tier is None:
                continue
            out.append(Workload(
                d.metadata.name, ns, tier, "Deployment",
                _pod_millis(d.spec.template), d.spec.replicas or 0))
        except Exception as e:
            log.warning("skipping deployment %s: %s", getattr(d.metadata, "name", "?"), e)
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


def free_millis(timeout_s: float = 5.0) -> float:
    core = client.CoreV1Api()
    alloc = sum(parse_cpu(n.status.allocatable["cpu"])
                for n in core.list_node(_request_timeout=timeout_s).items)
    used = 0
    for p in core.list_pod_for_all_namespaces(_request_timeout=timeout_s).items:
        if p.spec.node_name and p.status.phase in ("Pending", "Running"):
            used += sum(parse_cpu((c.resources.requests or {}).get("cpu", "0"))
                        for c in p.spec.containers if c.resources)
    return float(alloc - used)


def _age_s(ts, now: float) -> Optional[float]:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return now - ts.timestamp()
    return now - float(ts)


def is_unschedulable(pod, min_age_s: float = 0.0, now: Optional[float] = None) -> bool:
    """Stuck waiting for room: PodScheduled=False/Unschedulable for at least `min_age_s`.
    Plain phase=Pending would also count pods that are only pulling images or starting, so every
    C1 scale-up by Lifeboat itself would look like C1 struggling and trip L3. The grace covers
    new C1 pods that wait a few seconds while the pods paused for them terminate."""
    now = time.time() if now is None else now
    for c in (pod.status.conditions or []) if pod.status else []:
        if c.type == "PodScheduled" and c.status == "False" and c.reason == "Unschedulable":
            age = _age_s(getattr(c, "last_transition_time", None), now)
            return age is None or age >= min_age_s          # no timestamp: cannot tell, count it
    return False


def c1_unschedulable_pods(cfg: Config) -> int:
    core = client.CoreV1Api()
    pods = core.list_namespaced_pod(cfg.workload_namespace, label_selector=f"{TIER_LABEL}=C1",
                                    field_selector="status.phase=Pending",
                                    _request_timeout=cfg.api_timeout_s).items
    now = time.time()
    return sum(1 for p in pods if is_unschedulable(p, cfg.c1_stuck_grace_s, now))
