"""P6 data pipeline: results/*.csv -> figures for the report (E1-E5).

Each experiment has one CSV contract. A plotter runs when its CSV exists, otherwise it
says which role owes the data, so the figure list doubles as a progress check.

  python scripts/plot_results.py            # writes results/figures/*.png (needs matplotlib)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES, FIG = ROOT / "results", ROOT / "results" / "figures"
COLORS = {"B0": "#9e9e9e", "B1": "#64b5f6", "B2": "#ffb74d", "lifeboat": "#2e7d32"}


def _rows(name: str) -> list[dict] | None:
    p = RES / name
    return list(csv.DictReader(open(p, newline=""))) if p.exists() else None


def e1_e2(plt) -> list[str]:
    rows = _rows("e1_e2_summary.csv")
    if not rows:
        return []
    short = {"B0": "B0\npriority", "B1": "B1\nspare room", "B2": "B2\nno warnings", "lifeboat": "Full\nLifeboat"}
    labels = [short.get(r["setup"], r["setup"]) for r in rows]
    colors = [COLORS.get(r["setup"], "#555") for r in rows]

    fig, ax = plt.subplots(figsize=(7, 3.8))
    vals = [float(r["e1_time_to_capacity_s"]) for r in rows]
    bars = ax.bar(labels, vals, color=colors)
    ax.axhline(30, ls="--", color="red", lw=1, label="target: under 30 s")
    ax.bar_label(bars, [f"{v:.0f} s" for v in vals])
    ax.set_ylabel("seconds from first alert to C1 capacity")
    ax.set_title("E1: time to capacity (lower is better)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIG / "e1_time_to_capacity.png", dpi=150)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
    served = [float(r["c1_served_pct"]) for r in rows]
    b = a1.bar(labels, served, color=colors)
    a1.bar_label(b, [f"{v:.0f}%" for v in served])
    a1.set_ylim(0, 110)
    a1.set_title("E2: C1 requests served (higher is better)")
    x = range(len(rows))
    a2.bar([i - 0.2 for i in x], [float(r["p50_ms"]) for r in rows], 0.4, label="typical (p50)", color="#90caf9")
    a2.bar([i + 0.2 for i in x], [float(r["p95_ms"]) for r in rows], 0.4, label="slowest (p95)", color="#1565c0")
    a2.set_xticks(list(x), labels)
    a2.set_ylabel("ms")
    a2.set_title("E2: C1 response time (lower is better)")
    a2.legend()
    fig.tight_layout()
    fig.savefig(FIG / "e2_service_quality.png", dpi=150)
    return ["e1_time_to_capacity.png", "e2_service_quality.png"]


# experiment -> (csv it needs, owner, plotter or None while the plot is not written yet)
EXPERIMENTS = {
    "E1/E2 time to capacity, service quality": ("e1_e2_summary.csv", "P5", e1_e2),
    "E3 work saved": ("e3_work_saved.csv", "P3", None),
    "E4 alert accuracy": ("e4_alerts.csv", "P2", None),
    "E5 crash tests": ("e5_crash.csv", "P3", None),
}


if __name__ == "__main__":
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("pip install matplotlib")
    FIG.mkdir(parents=True, exist_ok=True)
    for exp, (name, owner, fn) in EXPERIMENTS.items():
        if not (RES / name).exists():
            print(f"  waiting  {exp:<42} needs results/{name} from {owner}")
        elif fn is None:
            print(f"  TODO     {exp:<42} results/{name} present, plot not written yet")
        else:
            print(f"  done     {exp:<42} -> " + ", ".join(f"results/figures/{f}" for f in fn(plt)))
