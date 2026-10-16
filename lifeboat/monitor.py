"""Prometheus queries + C1 health (the measured, no-vote L3 trigger)."""
from __future__ import annotations

import logging
import math
from typing import Optional

import requests

from . import discovery
from .config import Config

log = logging.getLogger("lifeboat.monitor")


class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def query(self, q: str) -> Optional[float]:
        try:
            r = requests.get(f"{self.cfg.prometheus_url}/api/v1/query", params={"query": q}, timeout=3)
            res = r.json()["data"]["result"]
            if not res:
                return None
            v = float(res[0]["value"][1])
            return None if math.isnan(v) else v
        except Exception as e:                       # Prometheus down must not crash Lifeboat
            log.warning("prometheus query failed: %s", e)
            return None

    def cluster_rps(self) -> float:
        return self.query(self.cfg.q_cluster_rps) or 0.0

    def c1_rps(self) -> float:
        return self.query(self.cfg.q_c1_rps) or 0.0

    def c1_struggling(self) -> bool:
        p95 = self.query(self.cfg.q_c1_p95)
        slow = p95 is not None and p95 > self.cfg.c1_latency_target_s
        try:
            stuck = discovery.c1_unschedulable_pods(self.cfg) > 0
        except Exception as e:
            log.warning("pending-pod check failed: %s", e)
            stuck = False
        return slow or stuck
