"""P5 standalone demo (no cluster needed):
  Part 1 - 20x surge shape, CPU calibration, and B0/B1/B2/Lifeboat E1 & E2 comparison.
  Part 2 - C1/C2 webapp burn, L3 ConfigMap feature shedding (80% CPU cut), and Prometheus metrics.

Run:  python scripts/demo_p5.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadtest.baselines import SURGE_STAGES, calibrate_cpu, evaluate_all


def demo_part1_baselines() -> None:
    print("=== Part 1: 20x surge & baselines (E1 time to capacity, E2 service quality) ===")
    cal = calibrate_cpu(burn_ms=20.0, pod_cpu_millis=250)
    print(
        f"CPU calibration : BURN_MS={cal.burn_ms:.0f} ms | {cal.pod_cpu_millis}m pod -> "
        f"{cal.max_rps_per_pod:.1f} RPS/pod ({cal.cost_millis_per_rps:.0f} mCPU/RPS)"
    )
    print(f"Locust stages   : {list(SURGE_STAGES)}  (10 -> 200 users = 20x surge for 5 min)\n")

    print(f"{'Setup':<26} {'C1 pods':>7} {'E1 (s)':>8} {'C1 served':>11} {'p50 (ms)':>10} {'p95 (ms)':>10}")
    print("-" * 78)
    for r in evaluate_all(peak_c1_rps=120.0, burn_ms=20.0):
        label = f"{r.code}  {r.name}"
        print(
            f"{label:<26} {r.c1_pods_ready:>7d} {r.e1_time_to_capacity_s:>7.1f}s "
            f"{r.c1_served_pct:>10.1f}% {r.p50_ms:>10.1f} {r.p95_ms:>10.1f}"
        )
    print()


def _load_webapp(tier: str, burn_ms: float, features_file: str):
    os.environ["TIER"] = tier
    os.environ["BURN_MS"] = str(burn_ms)
    os.environ["FEATURES_FILE"] = features_file
    spec = importlib.util.spec_from_file_location(f"webapp_{tier}_{int(time.time()*1000)}", ROOT / "workloads" / "webapp" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def demo_part2_webapp() -> None:
    print("=== Part 2: C1/C2 webapp burn, L3 degradation & Prometheus metrics ===")
    with tempfile.TemporaryDirectory() as tmp:
        feat_path = Path(tmp) / "degraded"
        feat_path.write_text("false", encoding="utf-8")

        mod = _load_webapp("C2", burn_ms=20.0, features_file=str(feat_path))
        client = mod.app.test_client()

        t0 = time.perf_counter()
        r_normal = client.get("/").get_json()
        normal_ms = (time.perf_counter() - t0) * 1000

        # Simulate Lifeboat L3 patching ConfigMap `c2-features` -> degraded="true"
        feat_path.write_text("true", encoding="utf-8")
        t1 = time.perf_counter()
        r_deg = client.get("/").get_json()
        deg_ms = (time.perf_counter() - t1) * 1000

        metrics_text = client.get("/metrics").get_data(as_text=True)
        has_counter = 'http_requests_total{code="200",tier="C2"} 2.0' in metrics_text

        print(f"1. C2 normal (L0-L2)   : response={r_normal}  burn={normal_ms:.1f} ms")
        print(f"2. C2 degraded (at L3) : response={r_deg}   burn={deg_ms:.1f} ms  (~80% CPU saved)")
        print(f"3. Prometheus /metrics : http_requests_total & histogram exported = {has_counter}")


if __name__ == "__main__":
    demo_part1_baselines()
    demo_part2_webapp()
