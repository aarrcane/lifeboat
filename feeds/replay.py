"""Replay a recorded/synthetic scenario through the same signing+send path.
Demo data never gets its own vote: it just stands in for S1/S2 feeds.

  python -m feeds.replay data/scenario_flash_flood.jsonl --speed 10
Each line: {"t": seconds_from_start, "source": "S1", "level": 2}
"""
import argparse
import json
import time

from . import common

TTL = {"S1": 150.0, "S2": 330.0}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    events = [json.loads(l) for l in open(a.file) if l.strip()]
    start = time.time()
    for e in sorted(events, key=lambda e: e["t"]):
        time.sleep(max(0.0, start + e["t"] / a.speed - time.time()))
        r = common.make_reading(e["source"], e["level"], TTL.get(e["source"], 150.0) / a.speed)
        print(f"t={e['t']:>5}s {e['source']} L{e['level']} -> {common.send(r)}", flush=True)
