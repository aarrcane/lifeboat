"""Generate the Lifeboat Grafana dashboard (P6) as a ConfigMap for kube-prometheus-stack.

The Grafana sidecar in kube-prometheus-stack loads every ConfigMap labelled
`grafana_dashboard: "1"` in the monitoring namespace, so `make monitoring` only has to apply it.

  python scripts/make_dashboard.py            # writes deploy/monitoring/grafana-dashboard.yaml
  python scripts/make_dashboard.py --check    # exit 1 if the YAML is out of date (CI-friendly)

Layout (follows Grafana's dashboard best-practices guide: tell one story, most important
information top-left, rows in the order data flows, RED for the user-facing service,
meaningful colour, a description on every panel):

  Status now        alert level | C1 p95 | C1 on time | plan | active executor | takeovers
  The vote          one state timeline: Lifeboat level, S1, S2, S3 votes, C1 struggling
  Warning signals   forecast ratio | rejected readings | human overrides
  Users (C1, RED)   requests/s by tier | share served on time | p95 response time
  Capacity freed    triage plan steps | free CPU
  Controller (HA)   executor per pod | control-loop age | tick duration

Colour code, used the same way on every panel: blue = normal / good, yellow = watch,
orange = emergency, red = critical / bad. C1 (the tier Lifeboat protects) is always purple;
the tiers it pauses are greys, so C1 stands out wherever it appears.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lifeboat.config import Config  # noqa: E402

OUT = ROOT / "deploy" / "monitoring" / "grafana-dashboard.yaml"
CFG = Config()
# Data source picked by the "Data source" variable; kube-prometheus-stack provisions uid "prometheus".
DS = {"type": "prometheus", "uid": "${datasource}"}

# ---------------------------------------------------------------- colour code
GOOD, WATCH, EMERGENCY, CRITICAL = "blue", "yellow", "orange", "red"
C1_COLOUR = "purple"
OFF = "#8E8E8E"                                         # standby / not in use
TIER_COLOURS = {"C1": C1_COLOUR, "C2": "#CCCCDC", "C3": "#8E8E8E", "C4": "#5A5A5A"}

LEVEL_MAPPING = [{"type": "value", "options": {
    "0": {"text": "L0 Normal", "color": GOOD, "index": 0},
    "1": {"text": "L1 Watch", "color": WATCH, "index": 1},
    "2": {"text": "L2 Emergency", "color": EMERGENCY, "index": 2},
    "3": {"text": "L3 Critical", "color": CRITICAL, "index": 3}}}]
STRUGGLING_MAPPING = [{"type": "value", "options": {
    "0": {"text": "C1 OK", "color": GOOD, "index": 0},
    "1": {"text": "C1 struggling", "color": CRITICAL, "index": 1}}}]
EXECUTOR_MAPPING = [{"type": "value", "options": {
    "0": {"text": "Standby", "color": OFF, "index": 0},
    "1": {"text": "Active", "color": GOOD, "index": 1}}}]


def steps(*pairs) -> dict:
    """Threshold steps: steps(("blue", None), ("red", 0.5))."""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}


def fixed(colour: str) -> dict:
    return {"mode": "fixed", "fixedColor": colour}


def by_name(name: str, colour: str) -> dict:
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": fixed(colour)}]}


# ---------------------------------------------------------------- panel builders
# Each builder returns a dict; layout() below adds ids and grid positions.
# Every query here is also evaluated against a real Prometheus by scripts/p6_live_check.py,
# so no Grafana-only macros ($__range, $__rate_interval) in expressions.
def stat(title, desc, targets, w, *, unit="none", decimals=None, thresholds=None, mappings=None,
         color_mode="value", graph="none", text_mode="auto", no_value=None, overrides=None) -> dict:
    defaults = {"unit": unit, "color": {"mode": "thresholds"},
                "thresholds": thresholds or steps((GOOD, None)), "mappings": mappings or []}
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value:
        defaults["noValue"] = no_value
    return {"type": "stat", "title": title, "description": desc, "targets": targets, "w": w,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph, "textMode": text_mode,
                        "justifyMode": "center", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}


def series(title, desc, targets, w, *, unit="none", decimals=None, axis_label="", minimum=None,
           maximum=None, soft_max=None, no_value=None, thresholds=None, threshold_style="off", colour=None, overrides=None,
           bars=False, stepped=False, legend=True) -> dict:
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2,
              "fillOpacity": 60 if bars else 12, "gradientMode": "none" if bars else "opacity",
              "showPoints": "never", "spanNulls": False, "axisLabel": axis_label,
              "axisPlacement": "auto", "axisSoftMin": 0,
              "lineInterpolation": "stepAfter" if stepped else "smooth",
              "stacking": {"mode": "none", "group": "A"},          # no stacking: it hides data
              "thresholdsStyle": {"mode": threshold_style}}
    defaults = {"unit": unit, "custom": custom, "color": colour or {"mode": "palette-classic"},
                "thresholds": thresholds or steps(("transparent", None))}
    if soft_max is not None:
        custom["axisSoftMax"] = soft_max                  # keep quiet panels from autoscaling to 0-100
    for key, val in (("decimals", decimals), ("min", minimum), ("max", maximum), ("noValue", no_value)):
        if val is not None:
            defaults[key] = val
    return {"type": "timeseries", "title": title, "description": desc, "targets": targets, "w": w,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"showLegend": legend, "displayMode": "list", "placement": "bottom",
                                   "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def timeline(title, desc, targets, w, *, mappings, overrides=None, legend=False) -> dict:
    return {"type": "state-timeline", "title": title, "description": desc, "targets": targets, "w": w,
            # colour comes from the value mappings; a thresholds colour mode would override them
            "fieldConfig": {"defaults": {"color": fixed(OFF), "mappings": mappings,
                                         "custom": {"fillOpacity": 80, "lineWidth": 0}},
                            "overrides": overrides or []},
            "options": {"showValue": "auto", "mergeValues": True, "alignValue": "left",
                        "rowHeight": 0.8, "legend": {"showLegend": legend, "displayMode": "list",
                                                     "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def row(title: str) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "panels": []}


def q(expr: str, legend: str) -> tuple[str, str]:
    return (expr, legend)


C1_P95 = ('histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{tier="C1"}[1m])) by (le))')
C1_ON_TIME = (f'sum(rate(http_request_duration_seconds_bucket{{tier="C1", le="{CFG.c1_latency_target_s}"}}[1m]))'
              ' / sum(rate(http_request_duration_seconds_count{tier="C1"}[1m]))')
TARGET = CFG.c1_latency_target_s
X, Y = CFG.x_factor, CFG.y_factor

# (height, [panels]) per band, top to bottom. Widths in each band add up to 24.
BANDS = [
    (5, [
        stat("Alert level", "Lifeboat's current level. L0 normal, L1 watch (warm up C1), "
             "L2 emergency (pause batch work, pre-size C1), L3 critical (C1 is missing its target).",
             [q("max(lifeboat_level)", "level")], 6, mappings=LEVEL_MAPPING, color_mode="background",
             thresholds=steps((GOOD, None), (WATCH, 1), (EMERGENCY, 2), (CRITICAL, 3))),
        stat("C1 p95 response time", f"95th percentile response time of the public web tier C1 over "
             f"the last minute. The target is {TARGET} s; the number turns red above it.",
             [q(C1_P95, "C1 p95")], 4, unit="s", decimals=2, graph="area",
             thresholds=steps((GOOD, None), (CRITICAL, TARGET))),
        stat("C1 requests on time", f"Share of C1 requests answered within {TARGET} s over the last "
             "minute. Blue at 95 % or more, orange from 90 %, red below that.",
             [q(C1_ON_TIME, "on time")], 4, unit="percentunit", decimals=1, graph="area",
             thresholds=steps((CRITICAL, None), (EMERGENCY, 0.9), (GOOD, 0.95))),
        stat("Triage plan", "Steps in the current TriagePlan: done (applied to the cluster) and "
             "waiting. Waiting steps are normal for a few seconds after a level change.",
             [q('max(lifeboat_plan_steps{state="done"})', "done"),
              q('max(lifeboat_plan_steps{state="pending"})', "waiting")], 4, decimals=0,
             text_mode="value_and_name",
             overrides=[{"matcher": {"id": "byName", "options": "waiting"},
                         "properties": [{"id": "thresholds", "value": steps((GOOD, None), (WATCH, 1))}]}]),
        stat("Active executor", "The Lifeboat replica that holds the Lease and is allowed to act. "
             "The other replica waits on standby and takes over if this one dies.",
             [q("max by (pod) (lifeboat_is_active_executor) == 1", "{{pod}}")], 3,
             text_mode="name", no_value="None", color_mode="none"),
        stat("Takeovers", "How many times a standby replica has taken over the plan. It goes up by "
             "one after each executor kill test.",
             [q("max(lifeboat_takeover_number)", "takeovers")], 3, decimals=0, color_mode="none"),
    ]),
    (8, [
        timeline("How the vote reached this level", "Each lane is one input to the 2-of-3 vote, then "
                 "the level Lifeboat chose. S1 is PUB flood alerts, S2 NEA rainfall, S3 Lifeboat's own "
                 "traffic forecast. The last lane shows when C1 missed its target, which forces L3.",
                 [q("max(lifeboat_source_level{source=\"S1\"})", "S1 PUB flood alerts"),
                  q("max(lifeboat_source_level{source=\"S2\"})", "S2 NEA rainfall"),
                  q("max(lifeboat_source_level{source=\"S3\"})", "S3 traffic forecast"),
                  q("max(lifeboat_level)", "Lifeboat level"),
                  q("max(lifeboat_c1_struggling)", "C1 response time")], 24,
                 mappings=LEVEL_MAPPING,
                 overrides=[{"matcher": {"id": "byName", "options": "C1 response time"},
                             "properties": [{"id": "mappings", "value": STRUGGLING_MAPPING}]}]),
    ]),
    "Warning signals",
    (8, [
        series("Forecast traffic compared with normal", f"Lifeboat's Holt forecast of cluster traffic "
               f"divided by normal traffic (source S3). At {X:g}x it votes L1, at {Y:g}x it votes L2; "
               "the shaded bands mark those zones.",
               [q("max(lifeboat_forecast_ratio)", "forecast / normal")], 12, unit="suffix:×", decimals=1,
               colour=fixed(C1_COLOUR), threshold_style="dashed+area",
               thresholds=steps(("transparent", None), (WATCH, X), (EMERGENCY, Y))),
        series("Readings rejected", "Readings dropped at ingest in each 5-minute window, by reason. "
               "bad_signature means a forged reading; any of these raises the LifeboatForgedReadings alert.",
               [q("sum(increase(lifeboat_rejected_readings_total[5m])) by (reason)", "{{reason}}")], 6,
               decimals=0, bars=True, soft_max=3,
               overrides=[by_name("bad_signature", CRITICAL), by_name("expired", EMERGENCY),
                          by_name("malformed", WATCH), by_name("future", OFF)]),
        series("Human overrides", "Times an operator set the level by hand in each 5-minute window.",
               [q("sum(increase(lifeboat_overrides_total[5m]))", "overrides")], 6, decimals=0, bars=True, soft_max=3,
               colour=fixed(C1_COLOUR), legend=False),
    ]),
    "Users of the web service (C1)",
    (8, [
        series("Requests per second by tier", "Traffic each tier is serving. C1 is the public web "
               "service Lifeboat protects; the grey tiers are the work it pauses to free capacity.",
               [q("sum(rate(http_requests_total[1m])) by (tier)", "{{tier}}")], 8, unit="reqps",
               overrides=[by_name(t, c) for t, c in TIER_COLOURS.items()]),
        series("C1 requests on time", f"Share of C1 requests answered within {TARGET} s. Below the "
               "dashed 95 % line users start to notice.",
               [q(C1_ON_TIME, "on time")], 8, unit="percentunit", decimals=0, minimum=0, maximum=1,
               colour=fixed(C1_COLOUR), threshold_style="dashed", legend=False,
               thresholds=steps((CRITICAL, None), ("transparent", 0.95))),
        series("C1 p95 response time", f"95th percentile response time of C1. Above the {TARGET} s "
               "line (shaded red) Lifeboat treats C1 as struggling and moves to L3.",
               [q(C1_P95, "C1 p95")], 8, unit="s", decimals=2, colour=fixed(C1_COLOUR),
               threshold_style="dashed+area", legend=False, soft_max=TARGET * 1.2,
               thresholds=steps(("transparent", None), (CRITICAL, TARGET))),
    ]),
    "Capacity Lifeboat freed",
    (8, [
        series("Triage plan steps", "Steps in the TriagePlan over time. Done steps rise as batch "
               "jobs are checkpointed and paused; waiting steps should drop back to zero quickly.",
               [q("max(lifeboat_plan_steps) by (state)", "{{state}}")], 12, decimals=0, stepped=True,
               overrides=[by_name("done", GOOD), by_name("pending", WATCH)]),
        series("Free CPU in the cluster", "Allocatable CPU minus what pods have requested, in "
               "millicores. It rises when Lifeboat pauses work and falls as C1 scales up into it. "
               "Needs kube-state-metrics, so it is empty outside the cluster.",
               [q('1000 * (sum(kube_node_status_allocatable{resource="cpu"})'
                  ' - sum(kube_pod_container_resource_requests{resource="cpu"}))', "free CPU")], 12,
               unit="suffix: mCPU", decimals=0, colour=fixed(GOOD), legend=False,
               no_value="Shown in the cluster only (needs kube-state-metrics)"),
    ]),
    "Controller health",
    (7, [
        timeline("Executor role per replica", "Which Lifeboat replica is active over time. Exactly one "
                 "should be active; a gap means no one is acting, two at once raises an alert.",
                 [q("max by (pod) (lifeboat_is_active_executor)", "{{pod}}")], 12,
                 mappings=EXECUTOR_MAPPING),
        series("Control loop age", "Seconds since the control loop last finished a tick. Above "
               "30 s (shaded) the LifeboatLoopStalled alert fires.",
               [q("max(lifeboat_loop_age_seconds) by (pod)", "{{pod}}")], 6, unit="s", decimals=0, soft_max=35,
               threshold_style="dashed+area", thresholds=steps(("transparent", None), (CRITICAL, 30))),
        series("Control loop tick time", f"How long the last tick took. It must stay well under the "
               f"{CFG.lease_duration_s} s Lease, or the standby could take over a live executor.",
               [q("max(lifeboat_tick_seconds) by (pod)", "{{pod}}")], 6, unit="s", decimals=1,
               soft_max=CFG.lease_duration_s + 2,
               threshold_style="dashed", thresholds=steps(("transparent", None),
                                                          (CRITICAL, CFG.lease_duration_s))),
    ]),
]


def layout() -> list[dict]:
    """Turn BANDS into Grafana panels with ids and gridPos, top to bottom."""
    panels, y = [], 0
    for band in BANDS:
        if isinstance(band, str):
            p = row(band)
            p.update(id=len(panels) + 1, gridPos={"x": 0, "y": y, "w": 24, "h": 1})
            panels.append(p)
            y += 1
            continue
        h, items = band
        assert sum(p["w"] for p in items) == 24, [p["title"] for p in items]
        x = 0
        for item in items:
            p = dict(item)
            w = p.pop("w")
            targets = p.pop("targets")
            p.update(id=len(panels) + 1, datasource=DS, gridPos={"x": x, "y": y, "w": w, "h": h},
                     targets=[{"refId": chr(65 + j), "datasource": DS, "expr": e, "legendFormat": leg}
                              for j, (e, leg) in enumerate(targets)])
            panels.append(p)
            x += w
        y += h
    return panels


PANELS = layout()
VOTE_PANEL_ID = next(p["id"] for p in PANELS if p["title"] == "How the vote reached this level")


def dashboard() -> dict:
    return {
        "uid": "lifeboat-overview", "title": "Lifeboat: crisis triage", "schemaVersion": 39,
        "description": "Is there a crisis, what is Lifeboat doing about it, and is the public web "
                       "service (C1) still fast? Read top to bottom: status, the vote, signals, "
                       "users, capacity freed, controller health.",
        "editable": True, "graphTooltip": 1,               # shared crosshair across panels
        "refresh": "5s", "time": {"from": "now-30m", "to": "now"},   # Prometheus scrapes every 5 s
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "tags": ["lifeboat", "csc3104"],
        "templating": {"list": [{
            "name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
            "current": {"text": "Prometheus", "value": "prometheus"}, "hide": 0, "refresh": 1,
            "regex": "", "options": []}]},
        # Level changes: a thin marker on every graph, so cause and effect line up across panels.
        # Firing alerts last minutes and would tint every panel, so they go on the vote timeline only.
        "annotations": {"list": [{
            "name": "Level changes", "enable": True, "datasource": DS, "iconColor": "purple",
            "expr": "changes(max(lifeboat_level)[10s:5s]) > 0", "step": "5s",
            "titleFormat": "Alert level changed"},
            {"name": "Lifeboat alerts", "enable": True, "datasource": DS, "iconColor": "orange",
             "expr": 'ALERTS{alertname=~"Lifeboat.*", alertstate="firing", alertname!="LifeboatLevelChanged"}',
             "step": "5s", "titleFormat": "{{alertname}}", "textFormat": "{{severity}}",
             "filter": {"exclude": False, "ids": [VOTE_PANEL_ID]}}]},
        "panels": PANELS,
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
    n = sum(p["type"] != "row" for p in PANELS)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({n} panels in {len(PANELS) - n} rows)")
