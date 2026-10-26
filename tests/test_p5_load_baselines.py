"""Unit tests for P5: web workload, Locust 20x surge shape, CPU calibration, and B0-B2 baselines."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from lifeboat.config import Config
from loadtest.baselines import (
    BASELINES,
    SURGE_STAGES,
    calibrate_cpu,
    evaluate_all,
    evaluate_setup,
    parse_locust_stats_csv,
    surge_target_at,
)

ROOT = Path(__file__).resolve().parents[1]


_WEBAPP_MOD = None


def _load_webapp_with(tier: str, burn_ms: float, features_file: str):
    global _WEBAPP_MOD
    if _WEBAPP_MOD is None:
        spec = importlib.util.spec_from_file_location("webapp_mod", ROOT / "workloads" / "webapp" / "app.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        _WEBAPP_MOD = mod
    _WEBAPP_MOD.TIER = tier
    _WEBAPP_MOD.BURN_MS = burn_ms
    _WEBAPP_MOD.FEATURES_FILE = features_file
    return _WEBAPP_MOD


def test_surge_shape_20x_ramp_and_stop():
    # 0-120s: 10 users (normal)
    assert SURGE_STAGES[0] == (120, 10)
    assert surge_target_at(30) == (10, 50)
    # 120-480s: 200 users (20x surge)
    assert SURGE_STAGES[1] == (180, 200)
    assert SURGE_STAGES[2] == (480, 200)
    assert surge_target_at(240) == (200, 50)
    # 480-600s: back to 10 users, stops at 600s
    assert SURGE_STAGES[3] == (600, 10)
    assert surge_target_at(500) == (10, 50)
    assert surge_target_at(601) is None


def test_cpu_calibration_matches_planner_config():
    cfg = Config()
    cal = calibrate_cpu(burn_ms=20.0, pod_cpu_millis=250)
    assert cal.cost_millis_per_rps == cfg.cost_millis_per_rps == 20.0
    assert cal.max_rps_per_pod == 12.5


def test_webapp_c1_serves_and_exports_prometheus_metrics(tmp_path):
    feat = tmp_path / "degraded"
    feat.write_text("false", encoding="utf-8")
    mod = _load_webapp_with("C1", burn_ms=1.0, features_file=str(feat))
    client = mod.app.test_client()

    r = client.get("/")
    assert r.status_code == 200
    assert r.get_json() == {"tier": "C1", "degraded": False}
    assert client.get("/healthz").get_data(as_text=True) == "ok"

    m = client.get("/metrics").get_data(as_text=True)
    assert 'http_requests_total{code="200",tier="C1"} 1.0' in m
    assert 'http_request_duration_seconds_bucket{le="0.5",tier="C1"}' in m


def test_webapp_c2_degrades_at_l3_and_sheds_load(tmp_path):
    feat = tmp_path / "degraded"
    feat.write_text("false", encoding="utf-8")
    mod = _load_webapp_with("C2", burn_ms=5.0, features_file=str(feat))
    client = mod.app.test_client()

    assert client.get("/").get_json() == {"tier": "C2", "degraded": False}
    feat.write_text("true", encoding="utf-8")
    assert mod.degraded() is True
    assert client.get("/").get_json() == {"tier": "C2", "degraded": True}


def test_baselines_b0_b1_b2_lifeboat_defined():
    assert list(BASELINES.keys()) == ["B0", "B1", "B2", "lifeboat"]
    assert BASELINES["B0"].controller_replicas == 0
    assert BASELINES["B0"].placeholder_replicas == 0
    assert BASELINES["B1"].placeholder_replicas == 2
    assert BASELINES["B2"].controller_replicas == 2 and BASELINES["B2"].early_warning is False
    assert BASELINES["lifeboat"].controller_replicas == 2 and BASELINES["lifeboat"].early_warning is True


def test_e1_lifeboat_under_30s_and_beats_all_baselines():
    b0 = evaluate_setup("B0")
    b1 = evaluate_setup("B1")
    b2 = evaluate_setup("B2")
    lb = evaluate_setup("lifeboat")

    assert lb.e1_time_to_capacity_s < 30.0
    assert lb.e1_time_to_capacity_s < b2.e1_time_to_capacity_s < b1.e1_time_to_capacity_s < b0.e1_time_to_capacity_s


def test_e2_service_quality_and_csv_parser(tmp_path):
    results = {r.code: r for r in evaluate_all()}
    assert results["lifeboat"].c1_served_pct > results["B2"].c1_served_pct > results["B1"].c1_served_pct > results["B0"].c1_served_pct
    assert results["lifeboat"].p95_ms < results["B2"].p95_ms < results["B1"].p95_ms < results["B0"].p95_ms

    csv_file = tmp_path / "run_stats.csv"
    csv_file.write_text(
        "Type,Name,Request Count,Failure Count,50%,95%\n"
        "GET,C1,3600,36,24,180\n"
        "GET,C2,2400,120,28,420\n",
        encoding="utf-8",
    )
    parsed = parse_locust_stats_csv(csv_file, tier="C1")
    assert parsed["served_pct"] == 99.0
    assert parsed["p50_ms"] == 24.0
    assert parsed["p95_ms"] == 180.0
