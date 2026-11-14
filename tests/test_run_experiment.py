"""scripts/run_experiment.py: the maths that turn a sampled run into E1/E2/E3 rows (no cluster)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_experiment as rx  # noqa: E402
from loadtest.baselines import BASELINES, SURGE_STAGES  # noqa: E402


def row(t, ready, spec=None, level=0):
    return {"t": t, "c1_ready": ready, "c1_spec": spec if spec is not None else ready, "level": level}


def test_setups_match_the_baseline_definitions():
    for code, spec in BASELINES.items():
        s = rx.SETUPS[code]
        assert (s["lifeboat"], s["early_warning"], s["placeholders"], s["hpa"]) == \
            (spec.controller_replicas, spec.early_warning, spec.placeholder_replicas, spec.hpa), code
    assert rx.STAGES == ",".join(f"{e}:{u}" for e, u in SURGE_STAGES)
    assert rx.C1_NEEDED == 10                                    # 120 req/s x 20m / 250m


def test_scenario_alert_leads_the_surge_by_60s():
    ev = [json.loads(line) for line in open(ROOT / rx.SCENARIO) if line.strip()]
    first_l2 = min(e["t"] for e in ev if e["level"] == 2)
    assert rx.SURGE_START - first_l2 == 60
    assert {e["level"] for e in ev if e["t"] == rx.SURGE_END} == {0}


def test_surge_to_capacity():
    hpa = [row(t, 2 if t < 200 else 10) for t in range(0, 400, 10)]
    assert rx.surge_to_capacity_s(hpa) == 50
    early = [row(t, 2 if t < 120 else 12) for t in range(0, 400, 10)]
    assert rx.surge_to_capacity_s(early) == -30                  # ready 30 s before the surge
    assert rx.surge_to_capacity_s([row(t, 4) for t in range(0, 400, 10)]) is None


def test_alert_to_ready_and_ready_at_surge():
    rows = [row(0, 2), row(90, 2, 7, 2), row(95, 4, 7, 2), row(104, 7, 7, 2), row(160, 9, 12, 2)]
    assert rx.alert_to_ready_s(rows) == 14
    assert rx.alert_to_ready_s([row(0, 2), row(10, 2)]) is None
    assert rx.c1_ready_at(rows) == 7


def test_locust_result_lines_to_summary_row():
    log = ("noise\n"
           'RESULT {"name": "C1", "requests": 400, "failures": 100, "p50_ms": 30, "p95_ms": 900}\n'
           'RESULT {"name": "C2", "requests": 200, "failures": 0, "p50_ms": 25, "p95_ms": 80}\n')
    res = rx.parse_results(log)
    rows = [row(t, 2 if t < 200 else 10) for t in range(0, 400, 10)]
    s = rx.summary_row("B0", 1, rows, res)
    assert s == {"setup": "B0", "run": 1, "e1_surge_to_capacity_s": 50, "alert_to_ready_s": None,
                 "c1_ready_at_surge": 2, "c1_served_pct": 75.0, "p50_ms": 30, "p95_ms": 900,
                 "c2_served_pct": 100.0}
    assert rx.summary_row("lifeboat", 1, rows, {})["c1_served_pct"] is None


def test_e3_rows_and_csv(tmp_path):
    t0 = 1000.0
    events = [
        {"name": "c3-analytics", "kind": "start", "unit": 0, "ts": t0 - 50},
        {"name": "c3-analytics", "kind": "term", "unit": 2000, "ts": t0 + 90},
        {"name": "c3-analytics", "kind": "start", "unit": 1990, "ts": t0 + 420},
        {"name": "c4-backup", "kind": "start", "unit": 0, "ts": t0 - 50},
        {"name": "c4-backup", "kind": "start", "unit": 0, "ts": t0 - 5000},      # an older run
    ]
    out = rx.e3_rows("lifeboat", 2, events, t0 + rx.SURGE_END, since_ts=t0 - 100)
    assert out == [
        {"setup": "lifeboat", "run": 2, "workload": "c3-analytics", "progress_lost_s": 0.5, "restore_s": 90.0},
        {"setup": "lifeboat", "run": 2, "workload": "c4-backup", "progress_lost_s": 0, "restore_s": 0.0},
    ]
    p = tmp_path / "e3.csv"
    rx.append_csv(p, rx.E3_FIELDS, out)
    rx.append_csv(p, rx.E3_FIELDS, [{"setup": "B0", "run": 1, "workload": "c3-analytics",
                                     "progress_lost_s": 3, "restore_s": None}])
    lines = p.read_text().splitlines()
    assert lines[0] == ",".join(rx.E3_FIELDS) and lines[-1] == "B0,1,c3-analytics,3,"
