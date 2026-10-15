"""S1: PUB flood alerts (data.gov.sg). Serverless handler (OpenFaaS python3-http style).

TODO(owner): confirm the real response shape of the dataset in the proposal (ref [2]) and
set PUB_URL. The parser below is deliberately tolerant and is covered by a fixture test.
"""
import os

from . import common

PUB_URL = os.getenv("PUB_URL", "")
TTL_S = float(os.getenv("PUB_TTL_S", "150"))


def count_alerts(node) -> int:
    """Count items under any key that mentions 'alert' or 'flood'."""
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
