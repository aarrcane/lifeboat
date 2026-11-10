"""E5 crash tests on a real cluster -> results/e5_crash.csv (P6 with P3).

Run during a crisis replay (e.g. `make demo`), once Lifeboat is at L2 and a TriagePlan exists:

  python scripts/e5_crash_test.py --runs 3              # kill the active executor, 3 times
  python scripts/e5_crash_test.py --runs 3 --repeat 3   # kill it 3 times back to back per run

For each kill it measures:
  takeover_s           kill -> the standby holds the Lease AND has stamped a higher takeover number
  steps_left_half_done steps still not done in the TriagePlan after --settle seconds
  steps_mismatched     steps marked done whose workload is not in the target state (drift)
  takeover_increment   new takeover number - old (fencing: must be >= 1)
plus Lifeboat's own CPU/memory from `kubectl top` (needs metrics-server; k3s ships it).

Steps set a target state (suspend, replicas, ConfigMap data), so redoing one is harmless; the
proposal's "nothing done twice" is checked as "nothing drifted and nothing left half done".
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lifeboat.config import Config  # noqa: E402
from lifeboat.models import PlanRecord  # noqa: E402

FIELDS = ["test", "run", "takeover_s", "steps_total", "steps_left_half_done", "steps_mismatched",
          "takeover_increment", "lifeboat_cpu_m", "lifeboat_mem_mi"]
CFG = Config()


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout.strip()


def lease_holder() -> str:
    return kubectl("-n", CFG.system_namespace, "get", "lease", CFG.lease_name,
                   "-o", "jsonpath={.spec.holderIdentity}")


def plan() -> PlanRecord | None:
    try:
        obj = json.loads(kubectl("-n", CFG.system_namespace, "get", "triageplans.lifeboat.sit.edu",
                                 CFG.plan_name, "-o", "json"))
    except subprocess.CalledProcessError:
        return None
    return PlanRecord.from_spec(obj.get("spec", {}))


def parse_top(text: str) -> tuple[float, float]:
    """Mean CPU (millicores) and memory (MiB) from `kubectl top pod --no-headers` lines."""
    unit = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0}
    cpu, mem = [], []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            c, m = parts[1], parts[2]
            cpu.append(float(c[:-1]) if c.endswith("m") else float(c) * 1000)
            mem.append(float(m[:-2]) * unit.get(m[-2:], 1.0))
    return (sum(cpu) / len(cpu), sum(mem) / len(mem)) if cpu else (0.0, 0.0)


def mismatched(rec: PlanRecord) -> int:
    from kubernetes import config as kconfig
    from lifeboat.k8s_actions import K8sActions
    kconfig.load_kube_config()
    acts = K8sActions(CFG, checkpoints=None)
    return sum(1 for s in rec.steps.values() if s.done and acts._current(s) != acts._target(s))


def kill_and_measure(timeout: float, settle: float) -> dict:
    old_holder, before = lease_holder(), plan()
    if not old_holder or before is None:
        sys.exit("no active executor or no TriagePlan: start a crisis replay first (make demo)")
    t0 = time.time()
    kubectl("-n", CFG.system_namespace, "delete", "pod", old_holder, "--grace-period=0", "--force", "--wait=false")
    took, rec = None, before
    while time.time() - t0 < timeout:
        holder, rec = lease_holder(), plan() or rec
        if holder and holder != old_holder and rec.takeover > before.takeover:
            took = time.time() - t0
            break
        time.sleep(0.5)
    time.sleep(settle)
    rec = plan() or rec
    return {"takeover_s": round(took, 1) if took is not None else "",
            "steps_total": len(rec.steps),
            "steps_left_half_done": sum(not s.done for s in rec.steps.values()),
            "steps_mismatched": mismatched(rec),
            "takeover_increment": rec.takeover - before.takeover}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--repeat", type=int, default=1, help="kills back to back in one run")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--settle", type=float, default=30.0, help="seconds to let the new executor finish")
    ap.add_argument("--out", default=str(ROOT / "results" / "e5_crash.csv"))
    args = ap.parse_args()
    test = "kill-active" if args.repeat == 1 else f"kill-active-x{args.repeat}"
    out = Path(args.out)
    new = not out.exists()
    with open(out, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        for run in range(1, args.runs + 1):
            for _ in range(args.repeat):
                row = kill_and_measure(args.timeout, args.settle if _ == args.repeat - 1 else 0)
            cpu, mem = parse_top(kubectl("top", "pod", "-n", CFG.system_namespace, "-l", "app=lifeboat",
                                         "--no-headers"))
            row.update(test=test, run=run, lifeboat_cpu_m=round(cpu), lifeboat_mem_mi=round(mem))
            w.writerow(row)
            fh.flush()
            print(row)
    print(f"appended to {out}; plot with: python scripts/plot_results.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
