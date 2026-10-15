"""TriagePlan persistence. Interface + in-memory fake. K8s version: k8s_plan_store.py."""
from __future__ import annotations

import copy
import threading
from typing import Optional, Protocol

from .models import PlanRecord


class Fenced(Exception):
    """A newer executor (higher takeover number) owns the plan; our write is rejected."""


class PlanStore(Protocol):
    def get(self) -> Optional[PlanRecord]: ...
    def put(self, rec: PlanRecord) -> None:
        """Conditional write. Raises Fenced if stored takeover > rec.takeover."""


class InMemoryPlanStore:
    def __init__(self) -> None:
        self._rec: Optional[PlanRecord] = None
        self._lock = threading.Lock()

    def get(self) -> Optional[PlanRecord]:
        with self._lock:
            return copy.deepcopy(self._rec)

    def put(self, rec: PlanRecord) -> None:
        with self._lock:
            if self._rec is not None and rec.takeover < self._rec.takeover:
                raise Fenced(f"stored takeover {self._rec.takeover} > {rec.takeover}")
            self._rec = copy.deepcopy(rec)
