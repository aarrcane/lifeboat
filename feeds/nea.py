"""S2: NEA rainfall (data.gov.sg). Level from the max 5-min station reading.

Dataset ref [3]: https://api-open.data.gov.sg/v2/real-time/api/rainfall
Threshold justification (5-minute bucket totals):
  - NEA_L1_MM = 2.5 mm / 5 min (equiv. 30 mm/hr moderate-to-heavy downpour => L1 Watch)
  - NEA_L2_MM = 7.5 mm / 5 min (equiv. 90 mm/hr intense storm, exceeding NEA's 70 mm/hr
    Heavy Rain Warning criterion associated with flash floods => L2 Emergency evidence)
Supports both data.gov.sg v2 (`data.readings[].data[].value`) and v1 (`items[].readings[].value`).
"""
import os

from . import common

DEFAULT_NEA_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
NEA_URL = os.getenv("NEA_URL", DEFAULT_NEA_URL)
TTL_S = float(os.getenv("NEA_TTL_S", "330"))
L1_MM = float(os.getenv("NEA_L1_MM", "2.5"))     # 2.5 mm / 5 min = 30 mm/hr
L2_MM = float(os.getenv("NEA_L2_MM", "7.5"))     # 7.5 mm / 5 min = 90 mm/hr


def max_rain(payload) -> float:
    if not isinstance(payload, dict):
        return 0.0
    vals = []
    for rd in (payload.get("data", {}) or {}).get("readings", []):
        vals += [float(x["value"]) for x in rd.get("data", []) if x.get("value") is not None]
    for item in payload.get("items", []) or []:
        vals += [float(x["value"]) for x in item.get("readings", []) if x.get("value") is not None]
    return max(vals, default=0.0)


def level_from_payload(payload) -> int:
    m = max_rain(payload)
    return 2 if m >= L2_MM else 1 if m >= L1_MM else 0


def handle(event=None, context=None):
    level = level_from_payload(common.get_json(NEA_URL))
    return {"source": "S2", "level": level, "sent": common.send(common.make_reading("S2", level, TTL_S))}
