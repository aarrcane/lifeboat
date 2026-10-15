"""TriagePlan stored as a custom resource (=> etcd, replicated across the 3 k3s servers)."""
from __future__ import annotations

from typing import Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

from .models import PlanRecord
from .plan_store import Fenced

GROUP, VERSION, PLURAL, KIND = "lifeboat.sit.edu", "v1", "triageplans", "TriagePlan"


class K8sPlanStore:
    def __init__(self, namespace: str, name: str = "current"):
        self.ns, self.name = namespace, name
        self.api = client.CustomObjectsApi()

    def _read(self) -> Optional[dict]:
        try:
            return self.api.get_namespaced_custom_object(GROUP, VERSION, self.ns, PLURAL, self.name)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def get(self) -> Optional[PlanRecord]:
        obj = self._read()
        return PlanRecord.from_spec(obj.get("spec", {})) if obj else None

    def put(self, rec: PlanRecord) -> None:
        """Read-check-replace; resourceVersion makes it a compare-and-swap. Stale writer => Fenced."""
        for _ in range(3):
            cur = self._read()
            body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": KIND,
                    "metadata": {"name": self.name, "namespace": self.ns},
                    "spec": rec.to_spec()}
            try:
                if cur is None:
                    self.api.create_namespaced_custom_object(GROUP, VERSION, self.ns, PLURAL, body)
                    return
                if rec.takeover < int(cur.get("spec", {}).get("takeover", 0)):
                    raise Fenced("a newer executor owns the plan")
                body["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
                self.api.replace_namespaced_custom_object(GROUP, VERSION, self.ns, PLURAL, self.name, body)
                return
            except ApiException as e:
                if e.status == 409:
                    continue                      # lost the race, re-read and re-check fencing
                raise
        raise RuntimeError("could not write TriagePlan after retries")
