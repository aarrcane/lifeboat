"""E3 collector: print the workers' lifecycle events from the checkpoint store.

Run in the cluster by deploy/jobs/e3-collect.yaml (controller image + the lifeboat-s3 Secret):

  python -m lifeboat.e3_collect            # one `E3EVENT {...}` line per event, oldest first

scripts/run_experiment.py creates the Job, reads its log, deletes it, and turns the events into
`progress_lost_s` and `restore_s` with the functions below (standard library only, so the runner
works on a server without a venv).
"""
from __future__ import annotations

import json
import sys
from typing import Iterable, Optional

PREFIX = "E3EVENT "


def parse_log(text: str) -> list[dict]:
    """Events from the collector's log (other lines are ignored)."""
    out = []
    for line in text.splitlines():
        if line.startswith(PREFIX):
            out.append(json.loads(line[len(PREFIX):]))
    return sorted(out, key=lambda e: e["ts"])


def progress_lost_s(events: Iterable[dict], unit_seconds: float) -> float:
    """For each `term` followed by the next `start` of the same workload: the units the worker
    had reached minus the units it restarted from (work redone), times the unit length."""
    lost, pending_term = 0, None
    for e in sorted(events, key=lambda e: e["ts"]):
        if e["kind"] == "term":
            pending_term = e
        elif e["kind"] == "start" and pending_term is not None:
            lost += max(0, int(pending_term["unit"]) - int(e["unit"]))
            pending_term = None
    return round(lost * unit_seconds, 3)


def restore_s(events: Iterable[dict], crisis_end_ts: float) -> Optional[float]:
    """Seconds from the end of the crisis until the workload runs again: 0 if it was never
    interrupted, None if it was not back (last event is a `term`) within the wait."""
    evs = sorted(events, key=lambda e: e["ts"])
    if not any(e["kind"] == "term" for e in evs):
        return 0.0
    if evs[-1]["kind"] == "term":
        return None
    back = [e for e in evs if e["kind"] in ("start", "done") and e["ts"] >= crisis_end_ts]
    if not back:
        return 0.0                       # resumed before the crisis ended and kept running
    return round(back[-1]["ts"] - crisis_end_ts, 1)


def by_workload(events: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in events:
        out.setdefault(e["name"], []).append(e)
    return out


def main() -> int:
    from .checkpoint import from_env
    for ev in from_env().events():
        print(PREFIX + json.dumps(ev, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
