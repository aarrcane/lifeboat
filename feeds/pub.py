"""S1: PUB flood alerts (data.gov.sg). Serverless handler (OpenFaaS python3-http style).

Dataset ref [2]: https://data.gov.sg/datasets/d_f1404e08587ce555b9ea3f565e2eb9a3/view
Real-time endpoint: https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts
Response shape (v2 CAP records): `data.records` is a list of 2-minute observations ordered
newest-first, where `records[0].item.readings` lists active flood alert areas (and `msgType`
is "Alert" / "Update" unless cancelled with "Cancel"). Also supports legacy/fallback keys
containing 'alert' or 'flood'. Any active flood alert reports L2 evidence (which still requires
a second source to agree before Lifeboat enters L2 Emergency).
"""
import os

from . import common

DEFAULT_PUB_URL = "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts"
PUB_URL = os.getenv("PUB_URL", DEFAULT_PUB_URL)
TTL_S = float(os.getenv("PUB_TTL_S", "150"))


def count_alerts(node) -> int:
    """Count active flood alerts in data.gov.sg v2 `data.records` or under 'alert'/'flood' keys."""
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            records = data["records"]
            if not records:
                return 0
            latest_item = (records[0].get("item") if isinstance(records[0], dict) else None) or {}
            if isinstance(latest_item, dict):
                if str(latest_item.get("msgType", "")).lower() == "cancel":
                    return 0
                readings = latest_item.get("readings")
                if isinstance(readings, list):
                    return len(readings)
    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and any(t in k.lower() for t in ("alert", "flood")):
                n += len(v)
            else:
                n += count_alerts(v)
    elif isinstance(node, list):
        for v in node:
            n += count_alerts(v)
    return n


def level_from_payload(payload) -> int:
    return 2 if count_alerts(payload) > 0 else 0       # active flood alert => L2 evidence


def handle(event=None, context=None):
    level = level_from_payload(common.get_json(PUB_URL))
    return {"source": "S1", "level": level, "sent": common.send(common.make_reading("S1", level, TTL_S))}
