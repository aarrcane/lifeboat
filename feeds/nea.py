"""S2: NEA rainfall (data.gov.sg). Level from the max 5-min station reading.

TODO(owner): set NEA_URL (ref [3], e.g. the real-time rainfall API) and tune thresholds.
Expected shape: {"data": {"readings": [{"data": [{"stationId": "S1", "value": 0.0}, ...]}]}}
"""
import os

from . import common

NEA_URL = os.getenv("NEA_URL", "")
TTL_S = float(os.getenv("NEA_TTL_S", "330"))
L1_MM = float(os.getenv("NEA_L1_MM", "2.5"))     # per 5 min - tune on recorded data
L2_MM = float(os.getenv("NEA_L2_MM", "7.5"))


def max_rain(payload) -> float:
    vals = []
    for rd in (payload.get("data", {}) or {}).get("readings", []):
        vals += [float(x["value"]) for x in rd.get("data", []) if x.get("value") is not None]
    return max(vals, default=0.0)


def level_from_payload(payload) -> int:
    m = max_rain(payload)
    return 2 if m >= L2_MM else 1 if m >= L1_MM else 0


def handle(event=None, context=None):
    level = level_from_payload(common.get_json(NEA_URL))
    return {"source": "S2", "level": level, "sent": common.send(common.make_reading("S2", level, TTL_S))}
