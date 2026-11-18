"""Human override shared by every replica (ConfigMap `lifeboat-override` in the system namespace).

`POST /override` can land on either pod. Kept in one pod's memory, an override would be ignored
when it reached the standby and forgotten at the next takeover. So the pod that receives it
writes it here, and every replica reads it at the start of each tick.

  data: {"level": "0".."3" or "" (no override), "who": "<operator>", "ts": "<epoch seconds>"}
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from .models import Level

NAME = "lifeboat-override"
TIMEOUT_S = 5


@dataclass(frozen=True)
class Override:
    level: Optional[Level]
    who: str = ""
    ts: float = 0.0

    def to_data(self) -> dict[str, str]:
        return {"level": "" if self.level is None else str(int(self.level)),
                "who": self.who, "ts": f"{self.ts:.3f}"}

    @classmethod
    def from_data(cls, data: Optional[dict]) -> "Override":
        data = data or {}
        raw = (data.get("level") or "").strip()
        try:
            ts = float(data.get("ts") or 0.0)
        except ValueError:
            ts = 0.0
        return cls(Level(int(raw)) if raw else None, data.get("who", ""), ts)


NONE = Override(None)


class InMemoryOverrideStore:
    def __init__(self) -> None:
        self._ov = NONE
        self._lock = threading.Lock()

    def get(self) -> Override:
        with self._lock:
            return self._ov

    def set(self, level: Optional[Level], who: str) -> Override:
        with self._lock:
            self._ov = Override(level, who, round(time.time(), 3))
            return self._ov


class K8sOverrideStore:
    def __init__(self, namespace: str, name: str = NAME):
        from kubernetes import client
        self.ns, self.name = namespace, name
        self.core = client.CoreV1Api()

    def get(self) -> Override:
        from kubernetes.client.rest import ApiException
        try:
            cm = self.core.read_namespaced_config_map(self.name, self.ns, _request_timeout=TIMEOUT_S)
        except ApiException as e:
            if e.status == 404:
                return NONE
            raise
        return Override.from_data(cm.data)

    def set(self, level: Optional[Level], who: str) -> Override:
        from kubernetes import client
        from kubernetes.client.rest import ApiException
        ov = Override(level, who, round(time.time(), 3))
        try:
            self.core.patch_namespaced_config_map(self.name, self.ns, {"data": ov.to_data()},
                                                  _request_timeout=TIMEOUT_S)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=self.name, namespace=self.ns),
                                      data=ov.to_data())
            self.core.create_namespaced_config_map(self.ns, body, _request_timeout=TIMEOUT_S)
        return ov
