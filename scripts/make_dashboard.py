"""Generate the Lifeboat Grafana dashboard (P6) as a ConfigMap for kube-prometheus-stack.

The Grafana sidecar in kube-prometheus-stack loads every ConfigMap labelled
`grafana_dashboard: "1"` in the monitoring namespace, so `make monitoring` only has to apply it.

  python scripts/make_dashboard.py            # writes deploy/monitoring/grafana-dashboard.yaml
  python scripts/make_dashboard.py --check    # exit 1 if the YAML is out of date (CI-friendly)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "deploy" / "monitoring" / "grafana-dashboard.yaml"
DS = {"type": "prometheus", "uid": "prometheus"}      # datasource uid provisioned by kube-prometheus-stack

LEVEL_MAPPING = [{"type": "value", "options": {
    "0": {"text": "L0 Normal", "color": "green"},
    "1": {"text": "L1 Watch", "color": "yellow"},
    "2": {"text": "L2 Emergency", "color": "orange"},
    "3": {"text": "L3 Critical", "color": "red"}}}]

# (title, panel type, [(PromQL, legend)], grid {x,y,w,h}, extra fieldConfig defaults)
# Every query here is also evaluated against a real Prometheus by scripts/p6_live_check.py.
PANELS = [
    ("Alert level", "stat", [("max(lifeboat_level)", "level")],
     {"x": 0, "y": 0, "w": 6, "h": 5}, {"mappings": LEVEL_MAPPING}),
    ("Active executor", "stat", [("max(lifeboat_is_active_executor) by (pod)", "{{pod}}")],
     {"x": 6, "y": 0, "w": 6, "h": 5},
     {"mappings": [{"type": "value", "options": {"0": {"text": "standby", "color": "blue"},
                                                  "1": {"text": "ACTIVE", "color": "green"}}}]}),
    ("Takeover number", "stat", [("max(lifeboat_takeover_number)", "takeover")],
     {"x": 12, "y": 0, "w": 6, "h": 5}, {}),
    ("C1 struggling (L3 trigger)", "stat", [("max(lifeboat_c1_struggling)", "struggling")],
     {"x": 18, "y": 0, "w": 6, "h": 5},
     {"mappings": [{"type": "value", "options": {"0": {"text": "OK", "color": "green"},
                                                  "1": {"text": "STRUGGLING", "color": "red"}}}]}),
    ("Alert level over time", "timeseries",
     [("max(lifeboat_level)", "Lifeboat level"),
      ("max(lifeboat_source_level) by (source)", "{{source}} vote")],
     {"x": 0, "y": 5, "w": 12, "h": 8}, {"min": 0, "max": 3, "decimals": 0}),
    ("Forecast traffic / normal (S3)", "timeseries",
     [("max(lifeboat_forecast_ratio)", "forecast ratio")],
     {"x": 12, "y": 5, "w": 12, "h": 8},
     {"thresholds": {"mode": "absolute", "steps": [
         {"color": "green", "value": None}, {"color": "yellow", "value": 2},
         {"color": "orange", "value": 4}]},
      "custom": {"thresholdsStyle": {"mode": "line"}}}),
    ("Requests per second by tier", "timeseries",
     [('sum(rate(http_requests_total[1m])) by (tier)', "{{tier}}")],
     {"x": 0, "y": 13, "w": 12, "h": 8}, {"unit": "reqps"}),
    ("C1 p95 response time (target 0.5 s)", "timeseries",
     [('histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{tier="C1"}[1m])) by (le))',
       "C1 p95")],
     {"x": 12, "y": 13, "w": 12, "h": 8},
     {"unit": "s", "thresholds": {"mode": "absolute", "steps": [
         {"color": "green", "value": None}, {"color": "red", "value": 0.5}]},
      "custom": {"thresholdsStyle": {"mode": "line"}}}),
    ("Triage plan steps", "timeseries",
     [("max(lifeboat_plan_steps) by (state)", "{{state}}")],
     {"x": 0, "y": 21, "w": 8, "h": 8}, {"decimals": 0}),
    ("Rejected readings (forged / expired)", "timeseries",
     [("sum(increase(lifeboat_rejected_readings_total[5m])) by (reason)", "{{reason}}")],
     {"x": 8, "y": 21, "w": 8, "h": 8}, {"decimals": 0}),
    ("Free CPU in cluster (millicores)", "timeseries",
     [('1000 * (sum(kube_node_status_allocatable{resource="cpu"})'
       ' - sum(kube_pod_container_resource_requests{resource="cpu"}))', "free mCPU")],
     {"x": 16, "y": 21, "w": 8, "h": 8}, {}),
]


def dashboard() -> dict:
    panels = []
    for i, (title, kind, targets, grid, defaults) in enumerate(PANELS, start=1):
        panels.append({
            "id": i, "title": title, "type": kind, "datasource": DS, "gridPos": grid,
            "targets": [{"refId": chr(65 + j), "datasource": DS, "expr": q, "legendFormat": leg}
                        for j, (q, leg) in enumerate(targets)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}} if kind == "stat" else {},
        })
    return {
        "uid": "lifeboat-overview", "title": "Lifeboat: crisis triage", "schemaVersion": 39,
        "refresh": "5s", "time": {"from": "now-30m", "to": "now"}, "tags": ["lifeboat", "csc3104"],
        "annotations": {"list": [{
            "name": "Level changes", "enable": True, "datasource": DS, "iconColor": "red",
            "expr": "changes(max(lifeboat_level)[30s:5s]) > 0", "step": "5s",
            "titleFormat": "Alert level changed"},
            {"name": "Lifeboat alerts", "enable": True, "datasource": DS, "iconColor": "orange",
             "expr": 'ALERTS{alertname=~"Lifeboat.*", alertstate="firing", alertname!="LifeboatLevelChanged"}',
             "step": "5s", "titleFormat": "{{alertname}}", "textFormat": "{{severity}}"}]},
        "panels": panels,
    }


def configmap_yaml() -> str:
    body = json.dumps(dashboard(), indent=2)
    indented = "\n".join("    " + line for line in body.splitlines())
    return (
        "# GENERATED by scripts/make_dashboard.py (P6). Edit the script, not this file.\n"
        "# Loaded automatically by the kube-prometheus-stack Grafana sidecar.\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: lifeboat-dashboard\n"
        "  namespace: monitoring\n"
        "  labels: {grafana_dashboard: \"1\"}\n"
        "data:\n"
        "  lifeboat.json: |\n"
        f"{indented}\n"
    )


if __name__ == "__main__":
    text = configmap_yaml()
    if "--check" in sys.argv:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("dashboard up to date" if ok else f"{OUT} is stale: run python scripts/make_dashboard.py")
        sys.exit(0 if ok else 1)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(PANELS)} panels)")
