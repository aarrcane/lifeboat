"""P6 data pipeline: results/*.csv -> figures for the report (E1-E5).

Each experiment has one CSV contract (docs/RESULTS_FORMAT.md). A plotter runs when its CSV
exists, otherwise it says who owes the data, so the figure list doubles as a progress check.

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


def _mean_by(rows: list[dict], key: str, col: str) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for r in rows:
        acc.setdefault(r[key], []).append(float(r[col]))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def e3(plt) -> list[str]:
    """results/e3_work_saved.csv: setup,run,workload,progress_lost_s,restore_s (docs/RESULTS_FORMAT.md)."""
    rows = _rows("e3_work_saved.csv")
    order = [s for s in ("B0", "B1", "B2", "lifeboat") if any(r["setup"] == s for r in rows)]
    lost, back = _mean_by(rows, "setup", "progress_lost_s"), _mean_by(rows, "setup", "restore_s")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
    for ax, vals, title in ((a1, lost, "E3: C3/C4 work lost (s, lower is better)"),
                            (a2, back, "E3: time until everything is back (s)")):
        b = ax.bar(order, [vals[s] for s in order], color=[COLORS.get(s, "#555") for s in order])
        ax.bar_label(b, [f"{vals[s]:.0f}" for s in order])
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(FIG / "e3_work_saved.png", dpi=150)
    return ["e3_work_saved.png"]


def e4(plt) -> list[str]:
    """results/e4_alerts.csv from scripts/export_e4.py."""
    rows = _rows("e4_alerts.csv")
    scen = list(dict.fromkeys(r["scenario"] for r in rows))
    get = lambda s, v, c: next(float(r[c]) for r in rows if r["scenario"] == s and r["voter"] == v)  # noqa: E731
    x = range(len(scen))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.8))
    for ax, col, title in ((a1, "false_l2_alarms", "E4: false L2 alarms (lower is better)"),
                           (a2, "flips", "E4: level changes in 30 min")):
        nv = [get(s, "naive", col) for s in scen]
        lb = [get(s, "lifeboat", col) for s in scen]
        b1 = ax.bar([i - 0.2 for i in x], nv, 0.4, label="naive (1 source)", color="#ef9a9a")
        b2 = ax.bar([i + 0.2 for i in x], lb, 0.4, label="Lifeboat (2 of 3)", color=COLORS["lifeboat"])
        ax.bar_label(b1, [f"{v:.0f}" for v in nv])
        ax.bar_label(b2, [f"{v:.0f}" for v in lb])
        ax.set_xticks(list(x), scen, fontsize=8)
        ax.set_title(title)
        ax.margins(y=0.15)
        ax.legend(fontsize=8)
    missed = sum(int(r["missed"]) for r in rows if r["voter"] == "lifeboat")
    fig.suptitle(f"Real crisis caught by Lifeboat: {'yes' if missed == 0 else 'NO'} "
                 f"(missed crises: {missed})", fontsize=10)
    fig.tight_layout()
    fig.savefig(FIG / "e4_alert_accuracy.png", dpi=150)
    return ["e4_alert_accuracy.png"]


def e5(plt) -> list[str]:
    """results/e5_crash.csv from scripts/e5_crash_test.py (docs/RESULTS_FORMAT.md)."""
    rows = _rows("e5_crash.csv")
    tests = list(dict.fromkeys(r["test"] for r in rows))
    take = _mean_by([r for r in rows if r["takeover_s"] != ""], "test", "takeover_s")
    bad = {t: sum(int(r["steps_left_half_done"]) + int(r["steps_mismatched"]) for r in rows if r["test"] == t)
           for t in tests}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.8))
    b = a1.bar(tests, [take.get(t, 0) for t in tests], color="#5c6bc0")
    a1.bar_label(b, [f"{take[t]:.0f} s" if t in take else "no takeover" for t in tests])
    a1.set_title("E5: time until triage continues (s)")
    a1.tick_params(axis="x", labelsize=8)
    b = a2.bar(tests, [bad[t] for t in tests], color=["#2e7d32" if bad[t] == 0 else "#c62828" for t in tests])
    a2.bar_label(b, [str(bad[t]) for t in tests])
    a2.set_title("E5: steps left half done or drifted (target 0)")
    a2.tick_params(axis="x", labelsize=8)
    cpu, mem = (sum(float(r[c]) for r in rows) / len(rows) for c in ("lifeboat_cpu_m", "lifeboat_mem_mi"))
    fig.suptitle(f"Lifeboat's own cost: {cpu:.0f} mCPU, {mem:.0f} MiB per replica (mean)", fontsize=10)
    fig.tight_layout()
    fig.savefig(FIG / "e5_crash_tests.png", dpi=150)
    return ["e5_crash_tests.png"]


# experiment -> (csv it needs, who produces it, plotter)
EXPERIMENTS = {
    "E1/E2 time to capacity, service quality": ("e1_e2_summary.csv", "P5", e1_e2),
    "E3 work saved": ("e3_work_saved.csv", "P3", e3),
    "E4 alert accuracy": ("e4_alerts.csv", "scripts/export_e4.py", e4),
    "E5 crash tests": ("e5_crash.csv", "P3 + P6", e5),
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
