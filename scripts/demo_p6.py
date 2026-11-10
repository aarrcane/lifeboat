"""P6 standalone demo (no cluster needed): observability & forecast.

  Part 1 - S3 forecast benchmark on three traffic shapes (lead time, false votes)
  Part 2 - alpha / beta / horizon tuning sweep (27 settings x 10 noise seeds)
  Part 3 - PromQL contract: the metric names and labels monitor.py queries really exist
           in the webapp and Lifeboat /metrics output; Prometheus-down safety
  Part 4 - Grafana dashboard: panels and the metrics they read

Run:  python scripts/demo_p6.py            (add --plots to write results/p6/*.png, needs matplotlib)
Live: python scripts/p6_live_check.py --prometheus <path>   (real Prometheus, see that file)
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from lifeboat.config import Config  # noqa: E402
from lifeboat.forecast_eval import TRACES, Params, benchmark, measured, replay, sweep  # noqa: E402
from lifeboat.monitor import Monitor  # noqa: E402

CFG = Config()
DEFAULT = Params(alpha=CFG.holt_alpha, beta=CFG.holt_beta, horizon=CFG.forecast_horizon_steps,
                 x=CFG.x_factor, y=CFG.y_factor, warmup=CFG.forecast_warmup_samples,
                 sample_s=CFG.forecast_sample_s)


def fmt(v, unit="s"):
    return "-" if v is None else f"{v:.0f}{unit}"


def part1() -> None:
    print("=== Part 1: S3 forecast benchmark (config defaults) ===")
    print(f"alpha={DEFAULT.alpha} beta={DEFAULT.beta} horizon={DEFAULT.horizon} x {DEFAULT.sample_s:.0f}s "
          f"= {DEFAULT.horizon * DEFAULT.sample_s / 60:.0f} min ahead | X={DEFAULT.x} (L1) Y={DEFAULT.y} (L2)\n")
    r = benchmark(DEFAULT)
    print(f"{'Traffic shape':<22}{'S3 votes L1':>12}{'S3 votes L2':>12}{'traffic 2x':>12}{'traffic 4x':>12}{'L2 lead':>10}")
    print("-" * 80)
    for name in ("locust_step", "gradual_flood"):
        s = r[name]
        print(f"{name:<22}{fmt(s.forecast_l1_s):>12}{fmt(s.forecast_l2_s):>12}{fmt(s.measured_l1_s):>12}"
              f"{fmt(s.measured_l2_s):>12}{fmt(s.lead_l2_s):>10}")
    q = r["no_crisis"]
    print(f"\nno_crisis (25 min, 2 short bursts): false L2 votes = {q.false_l2_samples}, "
          f"L1 votes = {q.false_l1_samples} samples, flips = {q.flips}")
    print("Takeaway: a step surge (Locust) gives no early signal, so S3 adds nothing there: PUB/NEA must warn.")
    print("On a gradual build-up S3 votes L2 before traffic reaches 4x. Bursts only reach L1 (one source,")
    print("cheap: 1 spare pod). L2 always needs a second source to agree.\n")


def part2() -> list[dict]:
    print("=== Part 2: tuning sweep (27 settings x 10 noise seeds) ===")
    rows = sweep(seeds=tuple(range(1, 11)))
    print(f"{'alpha':>6}{'beta':>6}{'horizon':>9}{'L2 lead':>9}{'false L2':>10}{"L1 votes*":>10}  verdict")
    print("-" * 64)
    for row in rows[:5] + [r for r in rows if (r["alpha"], r["beta"], r["horizon"]) == (0.5, 0.3, 12)]:
        tag = "chosen (config default)" if (row["alpha"], row["beta"], row["horizon"]) == \
            (DEFAULT.alpha, DEFAULT.beta, DEFAULT.horizon) else ("old default" if row["alpha"] == 0.5 and
                                                               row["beta"] == 0.3 and row["horizon"] == 12 else "")
        print(f"{row['alpha']:>6}{row['beta']:>6}{row['horizon']:>9}{row['lead_l2_s']:>8.0f}s"
              f"{row['false_l2']:>10}{row['false_l1']:>10}  {'safe' if row['safe'] else 'FALSE L2'} {tag}")
    unsafe = sum(not r["safe"] for r in rows)
    print(f"\n{unsafe} of {len(rows)} settings raise a false L2 vote on a normal day. Higher beta or a longer")
    print("horizon warns earlier but over-reacts to bursts. Old default alpha=0.5 did; alpha=0.3 does not.\n")
    return rows


def _webapp_metrics() -> str:
    from prometheus_client import REGISTRY
    for name in ("http_requests_total", "http_request_duration_seconds"):
        if name in REGISTRY._names_to_collectors:
            REGISTRY.unregister(REGISTRY._names_to_collectors[name])
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict("os.environ", {"TIER": "C1", "BURN_MS": "0", "FEATURES_FILE": f"{tmp}/x"}):
            spec = importlib.util.spec_from_file_location("p6_webapp", ROOT / "workloads/webapp/app.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            client = mod.app.test_client()
            client.get("/")
            return client.get("/metrics").get_data(as_text=True)


def part3() -> None:
    print("=== Part 3: PromQL contract & Prometheus-down safety ===")
    exposed = _webapp_metrics()
    from prometheus_client import generate_latest
    import lifeboat.metrics  # noqa: F401  (registers lifeboat_* gauges)
    exposed += generate_latest().decode()
    for name in ("q_cluster_rps", "q_c1_rps", "q_c1_p95"):
        q = getattr(CFG, name)
        metric = re.search(r"(http_[a-z_]+)", q).group(1)
        labels = set(re.findall(r"\{([a-z_]+)=", q))
        lines = [l for l in exposed.splitlines() if l.startswith(metric + "{")]
        has = bool(lines) and all(f'{lab}="' in lines[0] for lab in labels)
        print(f"  {'OK ' if has else 'MISSING'} {name:<14} needs {metric}{sorted(labels) or ''}")
    m = Monitor(Config(prometheus_url="http://127.0.0.1:1"))      # nothing listens on port 1
    with mock.patch("lifeboat.monitor.discovery.c1_unschedulable_pods", return_value=0):
        print(f"  Prometheus down: query -> {m.query(CFG.q_c1_p95)}, cluster_rps -> {m.cluster_rps()}, "
              f"c1_struggling -> {m.c1_struggling()}  (no crash, no false L3)\n")


def part4() -> None:
    from make_dashboard import OUT, PANELS
    print("=== Part 4: Grafana dashboard (deploy/monitoring/grafana-dashboard.yaml) ===")
    for title, kind, targets, _g, _d in PANELS:
        names = sorted(set(re.findall(r"(lifeboat_[a-z0-9_]+|http_[a-z0-9_]+|kube_[a-z0-9_]+)", " ".join(q for q, _ in targets))))
        print(f"  {kind:<10} {title:<38} {', '.join(names)}")
    print(f"  -> {len(PANELS)} panels, applied by `make monitoring`; file: {OUT.relative_to(ROOT)}\n")


def plots(rows: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out = ROOT / "results" / "p6"
    out.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for ax, name in zip(axes, ("gradual_flood", "no_crisis")):
        trace, dur = TRACES[name]
        run = replay(measured(trace, dur, DEFAULT.sample_s), DEFAULT)
        normal = [10.0] * len(run.times)
        ax.plot(run.times, run.rps, label="measured RPS (rate 1m)")
        ax.plot(run.times, run.predicted, label="Holt forecast (3 min ahead)", ls="--")
        ax.plot(run.times, [DEFAULT.x * n for n in normal], color="gold", lw=1, label="X = 2x normal (L1)")
        ax.plot(run.times, [DEFAULT.y * n for n in normal], color="tab:orange", lw=1, label="Y = 4x normal (L2)")
        l2 = [t for t, lv in zip(run.times, run.levels) if lv >= 2]
        if l2:
            ax.axvline(l2[0], color="red", ls=":", label=f"S3 votes L2 (t={l2[0]:.0f}s)")
        ax.set_title(name.replace("_", " "))
        ax.set_xlabel("seconds")
        ax.set_ylabel("requests / s")
        ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "forecast_traces.png", dpi=150)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for safe, color in ((True, "tab:green"), (False, "tab:red")):
        pts = [r for r in rows if r["safe"] == safe]
        ax.scatter([r["false_l1"] for r in pts], [r["lead_l2_s"] for r in pts], c=color,
                   label="0 false L2" if safe else "false L2 votes")
    best = rows[0]
    ax.annotate(f"chosen: a={best['alpha']} b={best['beta']} h={best['horizon']}",
                (best["false_l1"], best["lead_l2_s"]), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set_xlabel("L1 votes on no-crisis days (10 seeds)")
    ax.set_ylabel("L2 lead time on gradual flood (s)")
    ax.set_title("Forecast tuning: 27 settings")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "forecast_sweep.png", dpi=150)
    (out / "sweep.json").write_text(json.dumps(rows, indent=2))
    print(f"wrote {out}/forecast_traces.png, forecast_sweep.png, sweep.json")


if __name__ == "__main__":
    part1()
    rows = part2()
    part3()
    part4()
    if "--plots" in sys.argv:
        plots(rows)
