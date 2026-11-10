"""P6: forecast tuning, Prometheus-down safety, PromQL contract, Grafana dashboard."""
import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest import mock

import yaml

from lifeboat.config import Config
from lifeboat.forecast import TrafficForecaster
from lifeboat.forecast_eval import (Params, benchmark, gradual_surge_rps, locust_surge_rps, measured,
                                    no_crisis_rps, replay, score_quiet, sweep)
from lifeboat.models import Level
from lifeboat.monitor import Monitor

ROOT = Path(__file__).resolve().parents[1]
CFG = Config()
DEFAULT = Params(alpha=CFG.holt_alpha, beta=CFG.holt_beta, horizon=CFG.forecast_horizon_steps,
                 x=CFG.x_factor, y=CFG.y_factor, warmup=CFG.forecast_warmup_samples,
                 sample_s=CFG.forecast_sample_s)


# ---------------------------------------------------------------- forecast benchmark
def test_locust_trace_matches_p5_shape():
    assert locust_surge_rps(0) == 10 and locust_surge_rps(119) == 10
    assert locust_surge_rps(125) == 200 and locust_surge_rps(400) == 200
    assert locust_surge_rps(700) == 10


def test_measured_is_a_one_minute_rate():
    pts = dict(measured(locust_surge_rps, 300, 15, noise=0.0))
    assert pts[105] == 10 and 100 < pts[150] < 200 and pts[240] == 200   # rate()[1m] smooths the step


def test_default_forecast_warns_before_gradual_surge():
    s = benchmark(DEFAULT)["gradual_flood"]
    assert s.lead_l2_s is not None and s.lead_l2_s >= 60        # >= 1 min before traffic hits 4x


def test_default_forecast_never_votes_l2_on_normal_days():
    for seed in range(1, 11):
        run = replay(measured(no_crisis_rps, 1500, DEFAULT.sample_s, seed=seed), DEFAULT)
        assert score_quiet(run).false_l2_samples == 0, seed


def test_old_default_alpha_was_unsafe_and_new_default_wins_sweep():
    rows = sweep(seeds=tuple(range(1, 11)))
    old = next(r for r in rows if (r["alpha"], r["beta"], r["horizon"]) == (0.5, 0.3, 12))
    assert not old["safe"]
    best = rows[0]
    assert (best["alpha"], best["beta"], best["horizon"]) == (CFG.holt_alpha, CFG.holt_beta,
                                                              CFG.forecast_horizon_steps)


def test_step_surge_gives_no_lead_honestly():
    # A forecast cannot predict a step: that is the job of the PUB/NEA warnings.
    assert benchmark(DEFAULT)["locust_step"].lead_l2_s == 0


# ---------------------------------------------------------------- monitor safety
def test_prometheus_down_returns_none_and_no_false_l3():
    m = Monitor(Config(prometheus_url="http://127.0.0.1:1"))
    with mock.patch("lifeboat.monitor.discovery.c1_unschedulable_pods", return_value=0):
        assert m.query("up") is None
        assert m.cluster_rps_sample() is None and m.cluster_rps() == 0.0
        assert m.c1_struggling() is False


def _resp(result):
    r = mock.Mock()
    r.json.return_value = {"status": "success", "data": {"result": result}}
    return r


def test_monitor_parses_values_empty_and_nan():
    m = Monitor(CFG)
    with mock.patch("lifeboat.monitor.requests.get", return_value=_resp([{"value": [0, "12.5"]}])):
        assert m.query("x") == 12.5
    with mock.patch("lifeboat.monitor.requests.get", return_value=_resp([])):
        assert m.query("x") is None
    with mock.patch("lifeboat.monitor.requests.get", return_value=_resp([{"value": [0, "NaN"]}])):
        assert m.query("x") is None                  # histogram_quantile with no traffic


def test_no_data_does_not_poison_the_baseline():
    # Regression found by scripts/p6_live_check.py: missing samples used to be fed as 0.0.
    f = TrafficForecaster(CFG.holt_alpha, CFG.holt_beta, CFG.forecast_horizon_steps, 2, 4, 8)
    for _ in range(40):
        f.observe(10)
    m = Monitor(Config(prometheus_url="http://127.0.0.1:1"))
    for _ in range(40):                              # 10 min Prometheus outage
        rps = m.cluster_rps_sample()
        if rps is not None:
            f.observe(rps)
    for _ in range(5):
        f.observe(10)
    assert f.level() == Level.L0 and abs(f.baseline - 10) < 1


# ---------------------------------------------------------------- PromQL contract
def _forget_webapp_metrics() -> None:
    # Other tests load workloads/webapp/app.py too; its module-level metrics would collide.
    from prometheus_client import REGISTRY
    for name in ("http_requests_total", "http_request_duration_seconds"):
        coll = REGISTRY._names_to_collectors.get(name)
        if coll is not None:
            REGISTRY.unregister(coll)


def _exposition(tier: str) -> str:
    _forget_webapp_metrics()
    with mock.patch.dict("os.environ", {"TIER": tier, "BURN_MS": "0", "FEATURES_FILE": "/nonexistent"}):
        spec = importlib.util.spec_from_file_location(f"wa_{tier}", ROOT / "workloads/webapp/app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        c = mod.app.test_client()
        c.get("/")
        text = c.get("/metrics").get_data(as_text=True)
    _forget_webapp_metrics()                         # leave the registry as we found it for other tests
    return text


def test_monitor_promql_matches_webapp_metrics():
    text = _exposition("C1")
    assert re.search(r'^http_requests_total\{[^}]*tier="C1"', text, re.M)
    assert re.search(r'^http_request_duration_seconds_bucket\{[^}]*tier="C1"', text, re.M)
    assert re.search(r'le="0.5"', text)              # 0.5 s C1 target is a real bucket edge


# ---------------------------------------------------------------- dashboard
def _dashboard():
    doc = yaml.safe_load((ROOT / "deploy/monitoring/grafana-dashboard.yaml").read_text())
    assert doc["metadata"]["labels"]["grafana_dashboard"] == "1"
    return json.loads(doc["data"]["lifeboat.json"])


def test_dashboard_yaml_is_up_to_date():
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_dashboard
    assert (ROOT / "deploy/monitoring/grafana-dashboard.yaml").read_text() == make_dashboard.configmap_yaml()


def test_dashboard_covers_work_split_panels_and_known_metrics():
    from lifeboat import metrics as m
    from prometheus_client.metrics import MetricWrapperBase
    exported = {mm.describe()[0].name for mm in vars(m).values() if isinstance(mm, MetricWrapperBase)}
    d = _dashboard()
    exprs = " ".join(t["expr"] for p in d["panels"] for t in p["targets"])
    for needed in ("lifeboat_level", "lifeboat_source_level", "lifeboat_plan_steps",
                   "lifeboat_is_active_executor", "lifeboat_takeover_number", "http_request_duration_seconds"):
        assert needed in exprs
    for name in set(re.findall(r"lifeboat_[a-z0-9_]+", exprs)):
        base = name.removesuffix("_total")
        assert base in exported, f"dashboard uses unknown metric {name}"
    assert len({p["id"] for p in d["panels"]}) == len(d["panels"])


def test_alert_rules_are_picked_up_and_use_known_metrics():
    from prometheus_client.metrics import MetricWrapperBase
    from lifeboat import metrics as m
    exported = {mm.describe()[0].name for mm in vars(m).values() if isinstance(mm, MetricWrapperBase)}
    doc = yaml.safe_load((ROOT / "deploy/monitoring/alert-rules.yaml").read_text())
    assert doc["kind"] == "PrometheusRule" and doc["metadata"]["labels"]["release"] == "kps"
    rules = doc["spec"]["groups"][0]["rules"]
    names = {r["alert"] for r in rules}
    assert {"LifeboatLevelChanged", "LifeboatEmergency", "LifeboatNoActiveExecutor"} <= names
    for r in rules:
        for metric in re.findall(r"lifeboat_[a-z0-9_]+", r["expr"]):
            assert metric.removesuffix("_total") in exported, (r["alert"], metric)
    dash = _dashboard()
    assert any("ALERTS" in a["expr"] for a in dash["annotations"]["list"])   # alerts drawn on the dashboard


# ---------------------------------------------------------------- results pipeline
def test_e4_export_matches_p2_demo():
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_e4
    rows = export_e4.rows()
    lb = [r for r in rows if r["voter"] == "lifeboat"]
    assert sum(r["false_l2_alarms"] for r in lb) == 0 and sum(r["missed"] for r in lb) == 0
    assert sum(r["false_l2_alarms"] for r in rows if r["voter"] == "naive") > 0


def test_e5_kubectl_top_parsing():
    sys.path.insert(0, str(ROOT / "scripts"))
    from e5_crash_test import parse_top
    assert parse_top("lifeboat-a 12m 45Mi\nlifeboat-b 8m 40960Ki") == (10.0, 42.5)
    assert parse_top("") == (0.0, 0.0)


def test_every_plotter_runs_on_sample_csvs(tmp_path, monkeypatch):
    import pytest
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sys.path.insert(0, str(ROOT / "scripts"))
    import plot_results as pr
    (tmp_path / "figures").mkdir()
    (tmp_path / "e1_e2_summary.csv").write_text((ROOT / "results/e1_e2_summary.csv").read_text())
    (tmp_path / "e3_work_saved.csv").write_text(
        "setup,run,workload,progress_lost_s,restore_s\nB0,1,c3-analytics,120,90\nlifeboat,1,c3-analytics,10,60\n")
    import export_e4
    with open(tmp_path / "e4_alerts.csv", "w") as fh:
        fh.write(",".join(export_e4.FIELDS) + "\n")
        for r in export_e4.rows():
            fh.write(",".join(str(r[f]) for f in export_e4.FIELDS) + "\n")
    (tmp_path / "e5_crash.csv").write_text(
        "test,run,takeover_s,steps_total,steps_left_half_done,steps_mismatched,takeover_increment,"
        "lifeboat_cpu_m,lifeboat_mem_mi\nkill-active,1,17.5,4,0,0,1,12,45\n")
    monkeypatch.setattr(pr, "RES", tmp_path)
    monkeypatch.setattr(pr, "FIG", tmp_path / "figures")
    for _exp, (_csv, _owner, fn) in pr.EXPERIMENTS.items():
        for name in fn(plt):
            assert (tmp_path / "figures" / name).stat().st_size > 1000


def test_rejection_counters_exist_before_the_first_forgery():
    # increase() needs a sample before the first increment, or the first forged reading never alerts.
    from prometheus_client import generate_latest
    import lifeboat.metrics  # noqa: F401
    text = generate_latest().decode()
    for reason in ("malformed", "bad_signature", "expired"):
        assert f'lifeboat_rejected_readings_total{{reason="{reason}"}}' in text
