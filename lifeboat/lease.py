"""Leader election on a Kubernetes Lease. leaseTransitions doubles as the takeover number."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kubernetes import client
from kubernetes.client.rest import ApiException


class LeaseElector:
    def __init__(self, identity: str, name: str, namespace: str, duration_s: int = 15):
        self.identity, self.name, self.ns, self.duration = identity, name, namespace, duration_s
        self.api = client.CoordinationV1Api()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _expired(self, lease) -> bool:
        rt = lease.spec.renew_time or lease.spec.acquire_time
        if rt is None:
            return True
        return self._now() > rt + timedelta(seconds=lease.spec.lease_duration_seconds or self.duration)

    def try_acquire_or_renew(self) -> tuple[bool, int]:
        now = self._now()
        try:
            lease = self.api.read_namespaced_lease(self.name, self.ns)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1Lease(
                metadata=client.V1ObjectMeta(name=self.name, namespace=self.ns),
                spec=client.V1LeaseSpec(holder_identity=self.identity,
                                        lease_duration_seconds=self.duration,
                                        acquire_time=now, renew_time=now, lease_transitions=1))
            try:
                self.api.create_namespaced_lease(self.ns, body)
                return True, 1
            except ApiException as e2:
                if e2.status == 409:
                    return False, 0
                raise
        spec = lease.spec
        try:
            if spec.holder_identity == self.identity:
                spec.renew_time = now
                self.api.replace_namespaced_lease(self.name, self.ns, lease)
                return True, spec.lease_transitions or 1
            if self._expired(lease):
                spec.holder_identity = self.identity
                spec.acquire_time = spec.renew_time = now
                spec.lease_duration_seconds = self.duration
                spec.lease_transitions = (spec.lease_transitions or 0) + 1
                self.api.replace_namespaced_lease(self.name, self.ns, lease)  # resourceVersion CAS
                return True, spec.lease_transitions
        except ApiException as e:
            if e.status == 409:
                return False, 0                   # someone else won the race
            raise
        return False, spec.lease_transitions or 0

    def still_leader(self, takeover: int) -> bool:
        lease = self.api.read_namespaced_lease(self.name, self.ns)
        return (lease.spec.holder_identity == self.identity
                and (lease.spec.lease_transitions or 0) == takeover
                and not self._expired(lease))
