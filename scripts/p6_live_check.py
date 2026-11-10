"""P6 live check: a REAL Prometheus scraping the REAL webapp and Lifeboat's metrics, no cluster.

What runs for real
  * Prometheus (binary you pass with --prometheus) scraping every 5 s
  * two copies of workloads/webapp/app.py (C1 and C2) under a load generator
  * Lifeboat's sense-decide loop: Monitor (PromQL), TrafficForecaster (S3), Voter (2-of-3),
    planner, and the Executor with the in-memory plan store + fake actions from tests/fakes.py
  * S1/S2 come from data/scenario_flash_flood.jsonl, replayed SPEED times faster, signed
What it checks
  1. every PromQL query in lifeboat/config.py returns a value from real scraped data
  2. every Grafana panel query (scripts/make_dashboard.py) parses and returns series
  3. the forecast votes during the surge and the voter reaches L2 before peak traffic
  4. with Prometheus killed, Monitor returns None and the loop keeps ticking (no crash)
Outputs go to results/p6_live/: timeseries.csv, dashboard_queries.csv, summary.json, live_run.png

  curl -sSL https://github.com/prometheus/prometheus/releases/download/v2.53.2/prometheus-2.53.2.linux-amd64.tar.gz | tar xz
  python scripts/p6_live_check.py --prometheus ./prometheus-2.53.2.linux-amd64/prometheus
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from prometheus_client import start_http_server  # noqa: E402

import dataclasses  # noqa: E402

from lifeboat import discovery, ingest, metrics  # noqa: E402
from lifeboat.config import Config  # noqa: E402
from lifeboat.executor import Executor  # noqa: E402
from lifeboat.forecast import Holt, TrafficForecaster  # noqa: E402
from lifeboat.models import Level, Reading, Tier, Workload  # noqa: E402
from lifeboat.monitor import Monitor  # noqa: E402
from lifeboat.plan_store import InMemoryPlanStore  # noqa: E402
from lifeboat.planner import make_plan  # noqa: E402
from lifeboat.signing import sign  # noqa: E402
from lifeboat.voter import Voter  # noqa: E402
from make_dashboard import PANELS  # noqa: E402
from tests.fakes import FakeActions, FakeCluster, FakeElector, FakeLease  # noqa: E402

PROM_PORT, C1_PORT, C2_PORT, LB_PORT = 19090, 18001, 18002, 19100
log = logging.getLogger("p6")

WEBAPP_RUNNER = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("app", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import logging; logging.getLogger("werkzeug").setLevel(logging.ERROR)
m.app.run(host="127.0.0.1", port=int(sys.argv[2]), threaded=True)
"""


def prom_config(path: Path) -> None:
    path.write_text(f"""global: {{scrape_interval: 5s, evaluation_interval: 5s}}
scrape_configs:
  - job_name: webapp
    static_configs: [{{targets: ['127.0.0.1:{C1_PORT}', '127.0.0.1:{C2_PORT}']}}]
  - job_name: lifeboat
    static_configs: [{{targets: ['127.0.0.1:{LB_PORT}'], labels: {{pod: lifeboat-local-0}}}}]
""")


def wait_http(url: str, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(url, timeout=1).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


# ------------------------------------------------------------------ traffic shape (real seconds)
def target_rps(t: float, speed: float, normal: float, peak: float) -> float:
    """Scenario seconds: normal until 480, climb to peak by 720 (rain worsens), hold to 1140, drop."""
    s = t * speed
    if s < 480:
        return normal
    if s < 720:
        return normal + (peak - normal) * (s - 480) / 240
    if s < 1140:
        return peak
    return normal


def load_generator(stop: threading.Event, t0: float, args) -> None:
    pool, session = ThreadPoolExecutor(max_workers=48), requests.Session()
    owed = 0.0

    def hit(port: int) -> None:
        try:
            session.get(f"http://127.0.0.1:{port}/", timeout=2)
        except requests.RequestException:
            pass

    i = 0
    while not stop.is_set():
        owed += target_rps(time.time() - t0, args.speed, args.normal_rps, args.peak_rps) * 0.1
        while owed >= 1:
            owed -= 1
            i += 1
            pool.submit(hit, C1_PORT if i % 5 < 3 else C2_PORT)      # Locust task weights 3:2
        time.sleep(0.1)
    pool.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------------ Lifeboat sense-decide loop
def demo_workloads() -> list[Workload]:
    return [Workload("c1-emergency", "lifeboat-demo", Tier.C1, "Deployment", 250, 2),
            Workload("c2-portal", "lifeboat-demo", Tier.C2, "Deployment", 250, 2),
            Workload("c3-analytics", "lifeboat-demo", Tier.C3, "Job", 500, 2, last_checkpoint_ts=time.time()),
            Workload("c4-backup", "lifeboat-demo", Tier.C4, "Job", 500, 1, last_checkpoint_ts=time.time()),
            Workload("placeholder", "lifeboat-demo", Tier.PLACEHOLDER, "Deployment", 250, 2)]


def lifeboat_loop(stop: threading.Event, t0: float, cfg: Config, args, log_rows: list) -> None:
    discovery.c1_unschedulable_pods = lambda _cfg: 0            # no cluster: no pending C1 pods
    mon = Monitor(cfg)
    traffic = TrafficForecaster(cfg.holt_alpha, cfg.holt_beta, cfg.forecast_horizon_steps,
                                cfg.x_factor, cfg.y_factor, cfg.forecast_warmup_samples)
    c1_holt = Holt(cfg.holt_alpha, cfg.holt_beta)
    voter = Voter(cfg.hmac_secret, cfg.calm_minutes * 60, cfg.vote_window_s)
    lease = FakeLease()
    lease.holder, lease.transitions = "lifeboat-local-0", 1
    executor = Executor(InMemoryPlanStore(), FakeActions(FakeCluster()), FakeElector("lifeboat-local-0", lease))
    events = sorted((json.loads(l) for l in open(ROOT / "data" / "scenario_flash_flood.jsonl") if l.strip()),
                    key=lambda e: e["t"])
    ttl = {"S1": 150.0 / args.speed, "S2": 330.0 / args.speed}
    book = ingest.ReadingBook()
    workloads = demo_workloads()
    forged_sent = False

    def post(r: Reading) -> int:                                   # the real /readings handler
        code, _ = ingest.handle_post("/readings", json.dumps(dataclasses.asdict(r)).encode(), {},
                                     cfg.hmac_secret, book, lambda *_: None, now)
        return code

    while not stop.is_set():
        now = time.time()
        while events and events[0]["t"] / args.speed <= now - t0:              # replayed PUB / NEA
            e = events.pop(0)
            post(sign(Reading(e["source"], e["level"], now, now + ttl[e["source"]]), cfg.hmac_secret))
        if not forged_sent and (now - t0) * args.speed >= 400:                  # attacker forges an L2
            forged_sent = True
            code = post(Reading("S1", 2, now, now + 60, sig="forged"))
            log.info("forged S1=L2 reading -> HTTP %s (dropped before the voter)", code)
        rps, c1 = mon.cluster_rps_sample(), mon.c1_rps_sample()     # same as controller.step()
        if rps is not None:
            traffic.observe(rps)
        if c1 is not None:
            c1_holt.update(c1)
        rps = rps or 0.0
        book.add(sign(Reading("S3", int(traffic.level()), now, now + cfg.forecast_ttl_s), cfg.hmac_secret))
        struggling = mon.c1_struggling()
        readings = book.snapshot()
        level = voter.evaluate(readings, now, struggling)
        desired = make_plan(level, workloads, c1_holt.forecast(cfg.forecast_horizon_steps), 500.0, cfg, now)
        executor.tick(int(level), desired)
        rec = executor.store.get()
        metrics.LEVEL.set(int(level))
        metrics.C1_STRUGGLING.set(int(struggling))
        metrics.IS_ACTIVE.set(int(executor.is_active))
        metrics.FORECAST_RATIO.set(traffic.ratio())
        latest = {r.source: r.level for r in voter.valid_readings(readings, now)}
        for r in voter.valid_readings(readings, now):
            metrics.SOURCE_LEVEL.labels(r.source).set(r.level)
        if rec:
            done = sum(s.done for s in rec.steps.values())
            metrics.TAKEOVER.set(rec.takeover)
            metrics.PLAN_STEPS.labels("done").set(done)
            metrics.PLAN_STEPS.labels("pending").set(len(rec.steps) - done)
        log_rows.append({"t_real_s": round(now - t0, 1), "scenario_s": round((now - t0) * args.speed),
                         "cluster_rps": round(rps, 2), "forecast_ratio": round(traffic.ratio(), 2),
                         "S1": latest.get("S1"), "S2": latest.get("S2"),
                         "S3": int(traffic.level()), "level": int(level),
                         "plan_steps": len(rec.steps) if rec else 0})
        stop.wait(cfg.forecast_sample_s)


# ------------------------------------------------------------------ checks
def instant(q: str) -> dict:
    r = requests.get(f"http://127.0.0.1:{PROM_PORT}/api/v1/query", params={"query": q}, timeout=5).json()
    return {"status": r.get("status"), "error": r.get("error", ""),
            "series": len(r.get("data", {}).get("result", [])) if r.get("status") == "success" else 0}


def query_range(q: str, start: float, end: float) -> list[tuple[float, float]]:
    r = requests.get(f"http://127.0.0.1:{PROM_PORT}/api/v1/query_range",
                     params={"query": q, "start": start, "end": end, "step": "5s"}, timeout=10).json()
    res = r["data"]["result"]
    return [(float(t), float(v)) for t, v in res[0]["values"]] if res else []


def plot(rows: list[dict], series: dict, out: Path, t0: float) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, ax = plt.subplots(3, 1, figsize=(10, 7.5), sharex=True)
    for name, pts in series.items():
        if name.startswith("rps_") and pts:
            ax[0].plot([t - t0 for t, _ in pts], [v for _, v in pts], label=name[4:])
    ax[0].set_ylabel("requests/s\n(from Prometheus)")
    ax[0].legend(loc="upper left")
    ts = [r["t_real_s"] for r in rows]
    ax[1].plot(ts, [r["forecast_ratio"] for r in rows], color="tab:purple", label="forecast / normal")
    ax[1].axhline(2, ls="--", color="gold", label="X = 2 (L1)")
    ax[1].axhline(4, ls="--", color="tab:orange", label="Y = 4 (L2)")
    ax[1].set_ylabel("S3 forecast ratio")
    ax[1].legend(loc="upper left")
    for src, off, c in (("S1", -0.08, "tab:blue"), ("S2", 0.0, "tab:green"), ("S3", 0.08, "tab:purple")):
        ax[2].step(ts, [(r[src] if r[src] is not None else 0) + off for r in rows], where="post",
                   lw=1, color=c, alpha=0.7, label=f"{src} vote")
    ax[2].step(ts, [r["level"] for r in rows], where="post", lw=2.5, color="black", label="Lifeboat level")
    ax[2].set_yticks([0, 1, 2, 3], ["L0", "L1", "L2", "L3"])
    ax[2].set_xlabel("seconds (real time; scenario replayed faster)")
    ax[2].legend(loc="upper left", ncol=4, fontsize=8)
    fig.suptitle("P6 live check: real Prometheus, real webapp, Lifeboat voter + forecast")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prometheus", required=True, help="path to the prometheus binary")
    ap.add_argument("--speed", type=float, default=4.0, help="scenario replay speed")
    ap.add_argument("--normal-rps", type=float, default=10.0)
    ap.add_argument("--peak-rps", type=float, default=100.0)
    ap.add_argument("--out", default=str(ROOT / "results" / "p6_live"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # time-compressed settings (same code, shorter clocks), everything else from Config defaults
    cfg = Config(prometheus_url=f"http://127.0.0.1:{PROM_PORT}", forecast_sample_s=5.0,
                 calm_minutes=5.0 / args.speed, vote_window_s=600.0 / args.speed)
    duration = 1500 / args.speed
    procs, tmp = [], Path(tempfile.mkdtemp(prefix="p6-"))
    try:
        prom_config(tmp / "prometheus.yml")
        procs.append(subprocess.Popen([args.prometheus, f"--config.file={tmp / 'prometheus.yml'}",
                                       f"--storage.tsdb.path={tmp / 'data'}",
                                       f"--web.listen-address=127.0.0.1:{PROM_PORT}"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for tier, port in (("C1", C1_PORT), ("C2", C2_PORT)):
            env = dict(os.environ, TIER=tier, BURN_MS="2", FEATURES_FILE=str(tmp / "degraded"))
            procs.append(subprocess.Popen([sys.executable, "-c", WEBAPP_RUNNER,
                                           str(ROOT / "workloads" / "webapp" / "app.py"), str(port)], env=env))
        start_http_server(LB_PORT, addr="127.0.0.1")
        for url in (f"http://127.0.0.1:{PROM_PORT}/-/ready", f"http://127.0.0.1:{C1_PORT}/healthz",
                    f"http://127.0.0.1:{C2_PORT}/healthz"):
            wait_http(url)
        log.info("Prometheus + C1 + C2 up; running %.0f s (scenario x%.0f)", duration, args.speed)

        stop, rows, t0 = threading.Event(), [], time.time()
        # Normal traffic first, so rate()[1m] is full before Lifeboat learns its baseline. In a real
        # cluster Prometheus already has history; this only matters when everything starts together.
        warm = threading.Event()
        loader = threading.Thread(target=load_generator, args=(warm, time.time() - 10_000, args), daemon=True)
        loader.start()
        log.info("warming up: 75 s of normal traffic so rate()[1m] has a full window")
        time.sleep(75)
        warm.set()
        loader.join()
        t0 = time.time()
        threads = [threading.Thread(target=load_generator, args=(stop, t0, args), daemon=True),
                   threading.Thread(target=lifeboat_loop, args=(stop, t0, cfg, args, rows), daemon=True)]
        for th in threads:
            th.start()
        while time.time() - t0 < duration:
            time.sleep(10)
            if rows:
                r = rows[-1]
                log.info("t=%5.0fs rps=%6.1f ratio=%4.1f S1=%s S2=%s S3=%s -> L%s", r["t_real_s"],
                         r["cluster_rps"], r["forecast_ratio"], r["S1"], r["S2"], r["S3"], r["level"])
        t_end = time.time()

        # 1 + 2: PromQL from config.py and every dashboard panel, against real scraped data
        mon = Monitor(cfg)
        config_checks = {"q_cluster_rps": mon.query(cfg.q_cluster_rps), "q_c1_rps": mon.query(cfg.q_c1_rps),
                         "q_c1_p95": mon.query(cfg.q_c1_p95)}
        panel_rows = []
        for title, _kind, targets, _grid, _d in PANELS:
            for q, _legend in targets:
                res = instant(q)
                cluster_only = "kube_" in q
                panel_rows.append({"panel": title, "query": q, **res,
                                   "verdict": "ok" if res["series"] else
                                   ("cluster-only (kube-state-metrics)" if cluster_only and res["status"] == "success"
                                    else "NO DATA")})
        with open(out / "dashboard_queries.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(panel_rows[0]))
            w.writeheader()
            w.writerows(panel_rows)

        series = {f"rps_{tier}": query_range(f'sum(rate(http_requests_total{{tier="{tier}"}}[1m]))', t0, t_end)
                  for tier in ("C1", "C2")}
        series["c1_p95"] = query_range(cfg.q_c1_p95, t0, t_end)
        series["level"] = query_range("max(lifeboat_level)", t0, t_end)

        # 4: Prometheus down -> Monitor returns None, loop survives
        procs[0].terminate()
        procs[0].wait(timeout=10)
        time.sleep(1)
        down = {"cluster_rps_when_down": mon.cluster_rps(), "query_when_down": mon.query(cfg.q_c1_p95),
                "c1_struggling_when_down": mon.c1_struggling()}
        before = len(rows)
        time.sleep(cfg.forecast_sample_s * 2 + 1)
        down["loop_ticks_while_down"] = len(rows) - before
        stop.set()

        # 3: timeline facts
        first = lambda pred: next((r for r in rows if pred(r)), None)  # noqa: E731
        l2 = first(lambda r: r["level"] >= 2)
        s3 = first(lambda r: r["S3"] >= 1)
        peak = first(lambda r: r["cluster_rps"] >= 0.9 * args.peak_rps)
        summary = {
            "config_queries": config_checks,
            "config_queries_ok": all(v is not None for v in config_checks.values()),
            "dashboard_queries": len(panel_rows),
            "dashboard_ok": sum(r["verdict"] == "ok" for r in panel_rows),
            "dashboard_cluster_only": sum(r["verdict"].startswith("cluster-only") for r in panel_rows),
            "dashboard_no_data": [r["panel"] for r in panel_rows if r["verdict"] == "NO DATA"],
            "first_L2_scenario_s": l2["scenario_s"] if l2 else None,
            "first_S3_vote_scenario_s": s3["scenario_s"] if s3 else None,
            "traffic_90pct_peak_scenario_s": peak["scenario_s"] if peak else None,
            "max_level": max(r["level"] for r in rows),
            "final_level": rows[-1]["level"],
            "max_plan_steps": max(r["plan_steps"] for r in rows),
            "prometheus_down": down,
            "speed": args.speed, "samples": len(rows),
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        with open(out / "timeseries.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        drew = plot(rows, series, out / "live_run.png", t0)
        print(json.dumps(summary, indent=2))
        print(f"\nwrote {out}/summary.json, timeseries.csv, dashboard_queries.csv"
              + (", live_run.png" if drew else " (pip install matplotlib for live_run.png)"))
        ok = summary["config_queries_ok"] and not summary["dashboard_no_data"] and down["loop_ticks_while_down"] > 0
        return 0 if ok else 1
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    sys.exit(main())
