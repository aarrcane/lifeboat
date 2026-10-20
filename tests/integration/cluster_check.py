"""Cluster integration check: run with `make itest` (scripts/itest.sh), not by pytest.

Acts as the `lifeboat` ServiceAccount (so RBAC is tested too) against a real API server and
drives two controller instances, lb-a and lb-b, the way the two Lifeboat pods share the Lease
and the TriagePlan:
  1. L2 from two sources -> exactly one executor acts; plan written first; C4 paused, placeholders gone
  2. the active executor stops -> the standby takes the Lease, stamps takeover 2, the old one can't act
  3. readings expire -> level steps down, every step is undone, workloads match their originals

Needs only the API server (no images, no Prometheus): works on k3d, the AWS cluster, or a
control-plane-only k3s (`k3s server --disable-agent`, which CI uses).
Exit code 0 = all checks passed.
"""
import logging
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
os.environ.setdefault("LIFEBOAT_CHECKPOINT_BACKEND", "file")
os.environ.setdefault("LIFEBOAT_CHECKPOINT_DIR", tempfile.mkdtemp(prefix="lb-itest-"))
logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
logging.getLogger("lifeboat.monitor").setLevel(logging.ERROR)   # no Prometheus here, on purpose

from lifeboat.config import Config            # noqa: E402
from lifeboat.controller import Lifeboat      # noqa: E402
from lifeboat.models import Reading           # noqa: E402
from lifeboat.signing import sign             # noqa: E402

ADMIN = os.environ["ADMIN_KUBECONFIG"]        # used only to observe the cluster, never to act
SECRET = "itest"


def k(*args):
    return subprocess.run(["kubectl", "--kubeconfig", ADMIN, *args], capture_output=True, text=True).stdout.strip()


def cluster_state():
    get = lambda kind, name, path: k("-n", "lifeboat-demo", "get", kind, name, "-o", f"jsonpath={path}")  # noqa: E731
    return {
        "c3.suspend": get("job", "c3-analytics", "{.spec.suspend}"),
        "c4.suspend": get("job", "c4-backup", "{.spec.suspend}"),
        "c1.replicas": get("deploy", "c1-emergency", "{.spec.replicas}"),
        "placeholder.replicas": get("deploy", "placeholder", "{.spec.replicas}"),
        "c2.degraded": get("cm", "c2-features", "{.data.degraded}"),
    }


def plan():
    return k("-n", "lifeboat", "get", "triageplans", "current", "-o",
             "jsonpath={.spec.level} takeover={.spec.takeover} steps={range .spec.steps[*]}{.key}:{.done} {end}")


def lease():
    return k("-n", "lifeboat", "get", "lease", "lifeboat-executor", "-o",
             "jsonpath=holder={.spec.holderIdentity} transitions={.spec.leaseTransitions}")


def cfg(identity):
    return Config(identity=identity, prometheus_url="http://127.0.0.1:9", hmac_secret=SECRET,
                  lease_duration_s=6, checkpoint_wait_s=1.0, calm_minutes=0.05, tick_s=1)


def push(lb, level, ttl=600):
    now = time.time()
    for src in ("S1", "S2"):
        lb.book.add(sign(Reading(src, level, now, now + ttl), SECRET))


failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def main():
    before = cluster_state()
    if not before["c1.replicas"]:
        sys.exit("demo workloads not found: run `make deploy workloads` first")
    print("before:", before)
    a, b = Lifeboat(cfg("itest-a")), Lifeboat(cfg("itest-b"))
    push(a, 2)
    push(b, 2)

    print("\n1. L2 on both replicas, only the Lease holder acts")
    for _ in range(3):
        a.step()
        b.step()
        time.sleep(0.5)
    during = cluster_state()
    print("   lease:", lease())
    print("   plan: ", plan())
    check("exactly one executor active", a.executor.is_active != b.executor.is_active)
    active, standby = (a, b) if a.executor.is_active else (b, a)
    check("TriagePlan written at level 2", plan().startswith("2 "))
    check("every plan step done", ":false" not in plan() and ":true" in plan())
    check("C4 backup suspended", during["c4.suspend"] == "true")
    check("placeholders scaled to 0", during["placeholder.replicas"] == "0")

    print("\n2. active executor stops; standby takes over after the Lease expires")
    t0 = time.time()
    while time.time() - t0 < 30 and not standby.executor.is_active:
        standby.step()
        time.sleep(1)
    print(f"   took over after {time.time() - t0:.1f}s; lease: {lease()}")
    check("standby became active", standby.executor.is_active)
    check("Lease transitions went to 2", "transitions=2" in lease())
    check("plan stamped with takeover 2", "takeover=2" in plan())
    status = active.executor.tick(2, [])           # the old executor wakes up and tries to act
    check(f"old executor cannot act (got '{status}')", status in ("standby", "fenced"))
    check("cluster unchanged by the old executor", cluster_state() == during)

    print("\n3. crisis over: readings expire, level steps down, steps undone one per tick")
    standby.book = type(standby.book)()
    for _ in range(90):
        standby.step()
        time.sleep(1)
        if cluster_state() == before and plan().endswith("steps="):
            break
    print("   plan: ", plan())
    check("level back to 0", plan().startswith("0 "))
    check("every workload back to its original settings", cluster_state() == before)

    print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
