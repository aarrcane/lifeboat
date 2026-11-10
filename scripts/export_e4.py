"""E4 alert accuracy -> results/e4_alerts.csv (P6 data pipeline, P2's evaluation logic).

Runs the four recorded replay scenarios through the real Voter, for Lifeboat's 2-of-3 voter
and the naive 1-source voter, using scripts/demo_p2.py's evaluate_scenario so the numbers in
the report match P2's demo exactly.

  python scripts/export_e4.py && python scripts/plot_results.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from demo_p2 import evaluate_scenario  # noqa: E402

SCENARIOS = [  # (label, file, is a real crisis)
    ("Flash flood", "scenario_flash_flood.jsonl", True),
    ("Noisy sensor", "scenario_noisy.jsonl", False),
    ("Conflicting feeds", "scenario_conflicting.jsonl", False),
    ("No crisis", "scenario_no_crisis.jsonl", False),
]
FIELDS = ["scenario", "true_crisis", "voter", "peak_level", "false_l2_alarms", "false_l2_s", "missed", "flips"]


def rows() -> list[dict]:
    out = []
    for label, fname, crisis in SCENARIOS:
        for voter, naive in (("naive", True), ("lifeboat", False)):
            r = evaluate_scenario(ROOT / "data" / fname, crisis, naive=naive)
            out.append({"scenario": label, "true_crisis": int(crisis), "voter": voter,
                        "peak_level": r["peak"], "false_l2_alarms": r["false_alarms"],
                        "false_l2_s": r["false_l2_s"], "missed": r["missed"], "flips": r["flips"]})
    return out


if __name__ == "__main__":
    data = rows()
    path = ROOT / "results" / "e4_alerts.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(data)
    for r in data:
        print(f"  {r['scenario']:<18} {r['voter']:<9} peak L{r['peak_level']}  false L2 {r['false_l2_alarms']}"
              f"  missed {r['missed']}  flips {r['flips']}")
    print(f"wrote {path.relative_to(ROOT)}")
