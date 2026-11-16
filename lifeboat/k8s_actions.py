"""Idempotent Kubernetes actions: every apply() sets a TARGET STATE, never 'toggle'."""
from __future__ import annotations

import logging
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

TIMEOUT_S = 5            # every API call: a hung call must not stall the loop past the Lease

from .checkpoint import CheckpointStore
from .config import Config
from .models import Step
from .plan_store import Gone

log = logging.getLogger("lifeboat.actions")


class K8sActions:
    def __init__(self, cfg: Config, checkpoints: CheckpointStore):
        self.cfg, self.cp = cfg, checkpoints
        self.batch, self.apps, self.core = client.BatchV1Api(), client.AppsV1Api(), client.CoreV1Api()

    # state shape is identical for 'original', 'target' and 'current'
    def _current(self, s: Step) -> dict:
        try:
            return self._read(s)
        except ApiException as e:
            if e.status == 404:
                raise Gone(s.key) from e
            raise

    def _read(self, s: Step) -> dict:
        if s.action == "suspend_job":
            return {"suspend": bool(self.batch.read_namespaced_job(s.name, s.namespace, _request_timeout=TIMEOUT_S).spec.suspend)}
        if s.action == "scale_deployment":
            return {"replicas": self.apps.read_namespaced_deployment(s.name, s.namespace, _request_timeout=TIMEOUT_S).spec.replicas}
        if s.action == "set_configmap":
            data = self.core.read_namespaced_config_map(s.name, s.namespace, _request_timeout=TIMEOUT_S).data or {}
            return {"data": {k: data.get(k) for k in s.params["data"]}}
        raise ValueError(s.action)

    @staticmethod
    def _target(s: Step) -> dict:
        return {"suspend_job": {"suspend": True},
                "scale_deployment": {"replicas": s.params.get("replicas")},
                "set_configmap": {"data": s.params.get("data")}}[s.action]

    def read_original(self, s: Step) -> dict:
        return self._current(s)

    def _set(self, s: Step, state: dict) -> None:
        try:
            self._write(s, state)
        except ApiException as e:
            if e.status == 404:
                raise Gone(s.key) from e
            raise

    def _write(self, s: Step, state: dict) -> None:
        if s.action == "suspend_job":
            self.batch.patch_namespaced_job(s.name, s.namespace, {"spec": {"suspend": state["suspend"]}}, _request_timeout=TIMEOUT_S)
        elif s.action == "scale_deployment":
            self.apps.patch_namespaced_deployment_scale(
                s.name, s.namespace, {"spec": {"replicas": state["replicas"]}}, _request_timeout=TIMEOUT_S)
        elif s.action == "set_configmap":
            self.core.patch_namespaced_config_map(s.name, s.namespace, {"data": state["data"]}, _request_timeout=TIMEOUT_S)  # None => delete key

    def apply(self, s: Step) -> None:
        target = self._target(s)
        if self._current(s) == target:
            return                                         # already there: redo is a no-op
        self._set(s, target)

    def revert(self, s: Step) -> None:
        if self._current(s) != s.original:
            self._set(s, s.original)

    def is_reverted(self, s: Step) -> bool:
        return self._current(s) == s.original

    def verify(self, s: Step) -> bool:
        return self._current(s) == self._target(s)

    def checkpoint_wait_s(self) -> float:
        # the wait happens between lease renewals, so it must stay well inside the lease
        return min(self.cfg.checkpoint_wait_s, self.cfg.lease_duration_s / 2)

    def prepare(self, steps: list[Step]) -> None:
        """Ask every app about to be paused to save NOW, then wait ONCE for all of them.
        Only a fully-uploaded checkpoint (manifest written last) counts. Waiting per step
        would take N x wait and outlive the Lease."""
        asked = {}
        for s in steps:
            try:                                           # one bad step must not cancel the others
                if s.params.get("checkpoint") and self._current(s) != self._target(s):
                    asked[s.name] = self.cp.request(s.name)
            except Exception as e:
                log.warning("could not ask %s to checkpoint: %s", s.key, e)
        deadline = time.time() + self.checkpoint_wait_s()
        while asked and time.time() < deadline:
            for name, ts in list(asked.items()):
                try:
                    latest = self.cp.latest_ts(name)
                except Exception as e:
                    log.warning("checkpoint lookup for %s failed: %s", name, e)
                    latest = None
                if latest and latest >= ts:
                    del asked[name]
            if asked:
                time.sleep(0.5)
        if asked:
            log.warning("no fresh checkpoint for %s within %.0fs, pausing anyway (apps save on SIGTERM)",
                        sorted(asked), self.checkpoint_wait_s())
