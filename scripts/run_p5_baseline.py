"""Experiment runner for P5 baselines (B0, B1, B2, lifeboat).
Switches cluster configuration or runs in --dry-run mode and writes CSV summary to results/.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadtest.baselines import BASELINES, evaluate_all, evaluate_setup


def apply_cluster_baseline(code: str, dry_run: bool = False) -> list[str]:
    spec = BASELINES[code]
    cmds = [
        "bash scripts/reset.sh",
        f"kubectl -n lifeboat-demo scale deploy/placeholder --replicas={spec.placeholder_replicas}",
        f"kubectl -n lifeboat set env deploy/lifeboat LIFEBOAT_EARLY_WARNING={'true' if spec.early_warning else 'false'}",
        f"kubectl -n lifeboat scale deploy/lifeboat --replicas={spec.controller_replicas}",
    ]
    for c in cmds:
        print(f"  $ {c}")
        if not dry_run:
            subprocess.run(c, shell=True, check=True)
    return cmds


def write_results_csv(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = evaluate_all()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["setup", "name", "c1_pods_ready", "e1_time_to_capacity_s", "c1_served_pct", "p50_ms", "p95_ms"])
        for r in rows:
            w.writerow([r.code, r.name, r.c1_pods_ready, r.e1_time_to_capacity_s, r.c1_served_pct, r.p50_ms, r.p95_ms])


def main() -> None:
    ap = argparse.ArgumentParser(description="Run P5 baseline experiments (E1, E2).")
    ap.add_argument("--setup", choices=list(BASELINES.keys()) + ["all"], default="all")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="Print kubectl commands and write calibrated CSV without a cluster")
    ap.add_argument("--out", default=str(ROOT / "results" / "e1_e2_summary.csv"))
    args = ap.parse_args()

    codes = list(BASELINES.keys()) if args.setup == "all" else [args.setup]
    for code in codes:
        print(f"[{code}] {BASELINES[code].name} (runs={args.runs})")
        apply_cluster_baseline(code, dry_run=args.dry_run)
        res = evaluate_setup(code)
        print(f"  -> E1={res.e1_time_to_capacity_s}s | C1 served={res.c1_served_pct}% | p50={res.p50_ms}ms | p95={res.p95_ms}ms")

    out_path = Path(args.out)
    write_results_csv(out_path)
    print(f"Saved summary CSV to {out_path}")


if __name__ == "__main__":
    main()

