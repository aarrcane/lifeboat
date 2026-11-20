#!/usr/bin/env python3
"""Run and measure E1, E2 and E3 on a real cluster, for every setup (B0, B1, B2, lifeboat).

Standard library plus `kubectl` only, so it also runs on an EC2 server without a venv. Run it
on server-1 (in tmux) rather than a laptop far away: every kubectl call from Singapore to
us-east-1 takes 0.3-0.8 s, which ruins one-second sampling.

  python3 scripts/run_experiment.py --setup all --runs 3     # the full matrix (~11 min per run)
  python3 scripts/run_experiment.py --setup all --runs 1     # rehearsal
  python3 scripts/run_experiment.py --setup B0 --dry-run     # print what would be done

Timeline of one run (speed 1):

  t (s)  load (Locust)            warnings (replay; B2 and lifeboat only)
  0      10 users                 S1 = L0, S2 = L0
  75                              S2 = L1 (heavy rain)
  90                              S1 = L2, S2 = L2, repeated every 60 s until 330
  150    200 users (surge, 60 s after the L2 warning)
  330    10 users                 S1 = L0, S2 = L0
  360    Locust ends

Writes results/runs/<setup>-<run>/{timeline.csv,locust.log,e3.log}, appends one row per run to
results/e1_e2_summary.csv and one row per batch workload to results/e3_work_saved.csv
(columns in README.md, "Results files").
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lifeboat.e3_collect import by_workload, parse_log, progress_lost_s, restore_s  # noqa: E402

SYS, DEMO = "lifeboat", "lifeboat-demo"
SCENARIO = "data/scenario_experiment.jsonl"
STAGES = "150:10,330:200,360:10"
SURGE_START, SURGE_END, LOAD_END = 150, 330, 360
# Normal traffic runs this long before t = 0. Lifeboat is restarted 75 s into it (a full
# rate[1m] window of normal traffic), so its forecast baseline learns real normal traffic
# instead of the zeros Prometheus reports before Locust starts. Without this the L2 pre-size
# used a baseline of about 2.5 req/s instead of 6 (4 C1 pods instead of 7).
WARM_S, LIFEBOAT_RESTART_AT_S = 210, 75


def shifted(stages: str, by: int) -> str:
    """Locust STAGES with every end time moved `by` seconds later (the warm-up phase)."""
    return ",".join(f"{int(e) + by}:{u}" for e, u in (p.split(":") for p in stages.split(",")))
RECOVERY_WAIT_S = 480                  # after t = 330: at most this long for the workloads to be back
C1_PEAK_RPS, COST_MILLIS_PER_RPS, POD_MILLIS = 120, 20, 250
C1_NEEDED = math.ceil(C1_PEAK_RPS * COST_MILLIS_PER_RPS / POD_MILLIS)   # 10 ready pods = capacity
UNIT_SECONDS = 0.05                    # worker env UNIT_SECONDS in deploy/04-demo-workloads.yaml
BATCH = ("c3-analytics", "c4-backup")
HPA = "deploy/baselines/hpa-c1.yaml"

# setup -> (Lifeboat replicas, early warnings, placeholder replicas, HPA, replay warnings)
SETUPS = {
    "B0": dict(lifeboat=0, early_warning=False, placeholders=0, hpa=True, replay=False),
    "B1": dict(lifeboat=0, early_warning=False, placeholders=2, hpa=True, replay=False),
    "B2": dict(lifeboat=2, early_warning=False, placeholders=2, hpa=False, replay=True),
    "lifeboat": dict(lifeboat=2, early_warning=True, placeholders=2, hpa=False, replay=True),
}
SUMMARY_FIELDS = ["setup", "run", "e1_surge_to_capacity_s", "alert_to_ready_s", "c1_ready_at_surge",
                  "c1_served_pct", "p50_ms", "p95_ms", "c2_served_pct"]
E3_FIELDS = ["setup", "run", "workload", "progress_lost_s", "restore_s"]
TIMELINE_FIELDS = ["t", "c1_ready", "c1_spec", "level", "plan_steps", "c3_suspend", "c3_active",
                   "c4_suspend", "c4_active", "placeholder_ready"]
DRY = False
# set by prepare() for the experiments only; finish() removes them so demos and E5 run with the
# defaults the report describes (calm period 5 min, forecast warm-up 8 samples)
EXPERIMENT_ENV = ("LIFEBOAT_CALM_MINUTES=1", "LIFEBOAT_FORECAST_WARMUP_SAMPLES=4")
RESTORE_ENV = tuple(e.split("=")[0] + "-" for e in EXPERIMENT_ENV)


# ---------------------------------------------------------------- kubectl
def sh(*cmd: str, check: bool = True, env: Optional[dict] = None, stdin: Optional[str] = None,
       timeout: float = 300) -> str:
    if DRY:
        print("  $ " + " ".join(cmd))
        return ""
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


def kubectl(*args: str, check: bool = True, stdin: Optional[str] = None, timeout: float = 300) -> str:
    return sh("kubectl", *args, check=check, stdin=stdin, timeout=timeout)


def kjson(*args: str) -> dict:
    out = kubectl(*args, "-o", "json", check=False, timeout=10)
    try:
        return json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return {}


def create_from(path: str, **subs: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace(k, v)
    kubectl("create", "-f", "-", stdin=text)


def wait_until(what: str, fn, timeout: float, every: float = 2.0) -> bool:
    if DRY:
        print(f"  (wait: {what})")
        return True
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(every)
    print(f"  warning: timed out waiting for {what}")
    return False


# ---------------------------------------------------------------- prepare / start
def prepare(setup: str) -> None:
    s = SETUPS[setup]
    print(f"[{setup}] prepare")
    kubectl("-n", SYS, "delete", "cronjob", "pub-feed", "nea-feed", "--ignore-not-found")   # live feeds off
    kubectl("delete", "-f", str(ROOT / HPA), "--ignore-not-found")                        # never next to Lifeboat
    kubectl("-n", SYS, "delete", "job", "e3-collect", "--ignore-not-found", "--wait=true")
    kubectl("-n", SYS, "set", "env", "deploy/lifeboat",
            f"LIFEBOAT_EARLY_WARNING={'true' if s['early_warning'] else 'false'}", *EXPERIMENT_ENV)
    sh("bash", str(ROOT / "scripts" / "reset.sh"), env={"LIFEBOAT_REPLICAS": str(s["lifeboat"])},
       timeout=900)
    kubectl("-n", DEMO, "scale", "deploy/placeholder", f"--replicas={s['placeholders']}")
    if s["hpa"]:
        kubectl("apply", "-f", str(ROOT / HPA))
    kubectl("-n", DEMO, "rollout", "status", "deploy/c1-emergency", "--timeout=180s")
    kubectl("-n", DEMO, "rollout", "status", "deploy/c2-portal", "--timeout=180s")

    def batch_running():
        jobs = {j["metadata"]["name"]: j for j in kjson("-n", DEMO, "get", "jobs").get("items", [])}
        return all(jobs.get(n, {}).get("status", {}).get("active", 0) >= 1 for n in BATCH)
    wait_until("C3/C4 workers running", batch_running, 180)
    cm = kubectl("-n", SYS, "create", "configmap", "locustfile",
                 f"--from-file={ROOT / 'loadtest' / 'locustfile.py'}", "--dry-run=client", "-o", "yaml")
    kubectl("apply", "-f", "-", stdin=cm)


def start(setup: str) -> float:
    """Start Locust with a warm-up phase of normal traffic, restart Lifeboat during it so it
    learns that traffic, take t0 WARM_S after Locust started, then start the replay."""
    print(f"[{setup}] start")
    kubectl("-n", SYS, "delete", "job", "locust", "replay", "--ignore-not-found", "--wait=true")
    create_from("deploy/jobs/locust.yaml", __STAGES__=shifted(STAGES, WARM_S))

    def locust_running():
        pods = kjson("-n", SYS, "get", "pods", "-l", "job-name=locust").get("items", [])
        return any(p.get("status", {}).get("phase") == "Running" for p in pods)
    wait_until("Locust running", locust_running, 180, every=0.5)
    t_locust = time.time()
    if SETUPS[setup]["lifeboat"]:
        pause(LIFEBOAT_RESTART_AT_S - (time.time() - t_locust))
        kubectl("-n", SYS, "rollout", "restart", "deploy/lifeboat")
        kubectl("-n", SYS, "rollout", "status", "deploy/lifeboat", "--timeout=120s")
    left = WARM_S - (time.time() - t_locust)
    if left < 60 and not DRY:
        print(f"  warning: only {left:.0f} s of warm-up left after the Lifeboat restart")
    pause(left)
    t0 = time.time()
    if SETUPS[setup]["replay"]:
        create_from("deploy/jobs/replay.yaml", __SCENARIO__=SCENARIO, __SPEED__="1")
    return t0


def pause(seconds: float) -> None:
    if DRY:
        print(f"  (sleep {max(0, seconds):.0f} s)")
    elif seconds > 0:
        time.sleep(seconds)


# ---------------------------------------------------------------- sampling
def sample(t: float) -> dict:
    demo = kjson("-n", DEMO, "get", "deploy,jobs").get("items", [])
    plans = kjson("-n", SYS, "get", "triageplans.lifeboat.sit.edu").get("items", [])
    by = {(o["kind"], o["metadata"]["name"]): o for o in demo}

    def dep(name):
        o = by.get(("Deployment", name), {})
        return o.get("status", {}).get("readyReplicas", 0) or 0, o.get("spec", {}).get("replicas", 0) or 0

    def job(name):
        o = by.get(("Job", name), {})
        return int(bool(o.get("spec", {}).get("suspend"))), o.get("status", {}).get("active", 0) or 0
    spec = plans[0].get("spec", {}) if plans else {}
    c1r, c1s = dep("c1-emergency")
    c3s, c3a = job("c3-analytics")
    c4s, c4a = job("c4-backup")
    return {"t": round(t, 1), "c1_ready": c1r, "c1_spec": c1s, "level": int(spec.get("level", 0)),
            "plan_steps": len(spec.get("steps", [])), "c3_suspend": c3s, "c3_active": c3a,
            "c4_suspend": c4s, "c4_active": c4a, "placeholder_ready": dep("placeholder")[0]}


def back_to_normal(row: dict) -> bool:
    return (row["plan_steps"] == 0 and not row["c3_suspend"] and not row["c4_suspend"]
            and row["c3_active"] >= 1 and row["c4_active"] >= 1)


def locust_done() -> bool:
    st = kjson("-n", SYS, "get", "job", "locust").get("status", {})
    return bool(st.get("succeeded") or st.get("failed"))


def record(t0: float) -> list[dict]:
    rows, nxt = [], t0
    while True:
        now = time.time()
        row = sample(now - t0)
        rows.append(row)
        t = now - t0
        if t >= LOAD_END and locust_done() and back_to_normal(row):
            break
        if t >= SURGE_END + RECOVERY_WAIT_S:
            print("  warning: workloads not back within the wait")
            break
        if int(t) % 30 == 0:
            print(f"  t={t:5.0f}s level={row['level']} C1 {row['c1_ready']}/{row['c1_spec']} "
                  f"C3 susp={row['c3_suspend']} act={row['c3_active']} C4 susp={row['c4_suspend']} "
                  f"act={row['c4_active']}", flush=True)
        nxt += 1.0
        time.sleep(max(0.0, nxt - time.time()))
    return rows


# ---------------------------------------------------------------- maths (unit-tested)
def surge_to_capacity_s(rows: list[dict], needed: int = C1_NEEDED,
                        surge: float = SURGE_START) -> Optional[float]:
    """Seconds from the surge start until C1 has `needed` ready pods. Negative if they were ready
    before the surge (counted from the start of that streak). None if never."""
    at = [r for r in rows if r["t"] <= surge]
    if at and at[-1]["c1_ready"] >= needed:
        start = at[-1]["t"]
        for r in reversed(at):
            if r["c1_ready"] < needed:
                break
            start = r["t"]
        return round(start - surge, 1)
    for r in rows:
        if r["t"] > surge and r["c1_ready"] >= needed:
            return round(r["t"] - surge, 1)
    return None


def alert_to_ready_s(rows: list[dict]) -> Optional[float]:
    """Seconds from the first level change to all planned C1 pods ready (C1 scaled up and
    ready >= spec). None without an alert or if C1 never caught up."""
    i = next((i for i, r in enumerate(rows) if r["level"] > 0), None)
    if i is None:
        return None
    first = rows[i]
    base = rows[i - 1]["c1_spec"] if i > 0 else first["c1_spec"]      # C1 size before the alert
    for r in rows:
        if r["t"] >= first["t"] and r["c1_spec"] > base and r["c1_ready"] >= r["c1_spec"]:
            return round(r["t"] - first["t"], 1)
    return None


def c1_ready_at(rows: list[dict], t: float = SURGE_START) -> Optional[int]:
    before = [r for r in rows if r["t"] <= t]
    return before[-1]["c1_ready"] if before else None


def parse_results(log: str) -> dict[str, dict]:
    out = {}
    for line in log.splitlines():
        if line.startswith("RESULT "):
            r = json.loads(line[len("RESULT "):])
            out[r["name"]] = r
    return out


def served_pct(r: Optional[dict]) -> Optional[float]:
    if not r or not r.get("requests"):
        return None
    return round(100.0 * (r["requests"] - r["failures"]) / r["requests"], 1)


def summary_row(setup: str, run: int, rows: list[dict], results: dict[str, dict]) -> dict:
    c1, c2 = results.get("C1"), results.get("C2")
    return {"setup": setup, "run": run,
            "e1_surge_to_capacity_s": surge_to_capacity_s(rows),
            "alert_to_ready_s": alert_to_ready_s(rows) if SETUPS[setup]["lifeboat"] else None,
            "c1_ready_at_surge": c1_ready_at(rows),
            "c1_served_pct": served_pct(c1),
            "p50_ms": c1.get("p50_ms") if c1 else None, "p95_ms": c1.get("p95_ms") if c1 else None,
            "c2_served_pct": served_pct(c2)}


def e3_rows(setup: str, run: int, events: list[dict], crisis_end_ts: float, since_ts: float) -> list[dict]:
    per = by_workload(e for e in events if e["ts"] >= since_ts)
    out = []
    for name in BATCH:
        evs = per.get(name, [])
        out.append({"setup": setup, "run": run, "workload": name,
                    "progress_lost_s": progress_lost_s(evs, UNIT_SECONDS),
                    "restore_s": restore_s(evs, crisis_end_ts) if evs else None})
    return out


# ---------------------------------------------------------------- output
def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})


def collect_e3() -> str:
    kubectl("-n", SYS, "delete", "job", "e3-collect", "--ignore-not-found", "--wait=true")
    create_from("deploy/jobs/e3-collect.yaml")
    kubectl("-n", SYS, "wait", "--for=condition=complete", "job/e3-collect", "--timeout=120s", check=False)
    log = kubectl("-n", SYS, "logs", "job/e3-collect", check=False)
    kubectl("-n", SYS, "delete", "job", "e3-collect", "--ignore-not-found")
    return log


def one_run(setup: str, run: int, out: Path) -> None:
    t_prep = time.time()
    prepare(setup)
    t0 = start(setup)
    if DRY:
        print(f"[{setup}] (dry run: sample every second until t >= {LOAD_END} s and workloads are back)")
        return
    rows = record(t0)
    kubectl("-n", SYS, "wait", "--for=condition=complete", "job/locust", "--timeout=120s", check=False)
    log = kubectl("-n", SYS, "logs", "job/locust", check=False)
    time.sleep(10)                                         # let resumed workers write their start event
    e3_log = collect_e3()

    d = out / "runs" / f"{setup}-{run}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "timeline.csv").unlink(missing_ok=True)            # a re-run of the same number replaces it
    append_csv(d / "timeline.csv", TIMELINE_FIELDS, rows)
    (d / "locust.log").write_text(log, encoding="utf-8")
    (d / "e3.log").write_text(e3_log, encoding="utf-8")
    summ = summary_row(setup, run, rows, parse_results(log))
    append_csv(out / "e1_e2_summary.csv", SUMMARY_FIELDS, [summ])
    append_csv(out / "e3_work_saved.csv", E3_FIELDS,
               e3_rows(setup, run, parse_log(e3_log), t0 + SURGE_END, t_prep))
    print(f"[{setup}] run {run}: {summ}", flush=True)


def finish() -> None:
    """Leave the cluster as `make deploy workloads` would: no HPA, Lifeboat x2 with warnings on."""
    kubectl("delete", "-f", str(ROOT / HPA), "--ignore-not-found", check=False)
    kubectl("-n", SYS, "set", "env", "deploy/lifeboat", "LIFEBOAT_EARLY_WARNING=true", *RESTORE_ENV, check=False)
    kubectl("-n", SYS, "scale", "deploy/lifeboat", "--replicas=2", check=False)
    kubectl("-n", DEMO, "scale", "deploy/placeholder", "--replicas=2", check=False)


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--setup", choices=list(SETUPS) + ["all"], default="all")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--first-run", type=int, default=1, help="number of the first run (to resume a matrix)")
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--dry-run", action="store_true", help="print the kubectl commands, touch nothing")
    args = ap.parse_args()
    DRY = args.dry_run
    os.chdir(ROOT)
    setups = list(SETUPS) if args.setup == "all" else [args.setup]
    out = Path(args.out)
    try:
        for run in range(args.first_run, args.first_run + args.runs):   # interleave setups per run
            for setup in setups:
                one_run(setup, run, out)
    finally:
        if not DRY:
            finish()
    print(f"done: {out / 'e1_e2_summary.csv'}, {out / 'e3_work_saved.csv'}, {out / 'runs'}/; "
          "plot with: python scripts/plot_results.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
