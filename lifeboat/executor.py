"""Algorithm 3 - executing, with takeover-number fencing (Figure 2).

Safe to run on both replicas: only the Lease holder acts. Every step sets a target
state, so redoing it after a crash changes nothing.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from .models import PlanRecord, Step
from .plan_store import Fenced, PlanStore

log = logging.getLogger("lifeboat.executor")


class Actions(Protocol):
    def read_original(self, s: Step) -> dict: ...
    def apply(self, s: Step) -> None: ...
    def revert(self, s: Step) -> None: ...
    def verify(self, s: Step) -> bool: ...
    def is_reverted(self, s: Step) -> bool: ...


class Elector(Protocol):
    def try_acquire_or_renew(self) -> tuple[bool, int]: ...
    def still_leader(self, takeover: int) -> bool: ...


class Executor:
    def __init__(self, store: PlanStore, actions: Actions, elector: Elector):
        self.store, self.actions, self.elector = store, actions, elector
        self._takeover: Optional[int] = None

    @property
    def is_active(self) -> bool:
        return self._takeover is not None

    def tick(self, level: int, desired: list[Step]) -> str:
        leader, takeover = self.elector.try_acquire_or_renew()
        if not leader:
            self._takeover = None
            return "standby"
        try:
            if self._takeover != takeover:                       # just became active
                rec = self.store.get() or PlanRecord()
                rec.takeover = takeover                          # old executor is fenced now
                self.store.put(rec)
                self._takeover = takeover
                log.warning("became active executor, takeover=%s", takeover)
            rec = self.store.get() or PlanRecord(takeover=takeover)
            if rec.takeover > takeover:
                self._takeover = None
                return "fenced"
            self._sync(rec, level, desired)
            self._apply_pending(rec, takeover)
            self._reconcile(rec)
        except Fenced:
            self._takeover = None
            return "fenced"
        return "active"

    # -- 1. write the plan (with original settings) BEFORE acting
    def _sync(self, rec: PlanRecord, level: int, desired: list[Step]) -> None:
        changed = rec.level != level
        rec.level = level
        want = {s.key: s for s in desired}
        for key, s in want.items():
            cur = rec.steps.get(key)
            if cur is None:
                s.original = self.actions.read_original(s)
                s.done = False
                rec.steps[key] = s
                changed = True
            elif cur.params != s.params:
                cur.params, cur.done = s.params, False           # keep original settings
                changed = True
        stale = [k for k in rec.steps if k not in want]
        if stale:                                                # undo ONE step per tick, newest first
            st = rec.steps[stale[-1]]
            try:
                self.actions.revert(st)
                if self.actions.is_reverted(st):                 # checked against original settings
                    del rec.steps[st.key]
                    changed = True
            except Fenced:
                raise
            except Exception:
                log.exception("revert failed for %s, will retry", st.key)
        if changed:
            self.store.put(rec)

    # -- 2. do each unfinished step, mark it done
    def _apply_pending(self, rec: PlanRecord, takeover: int) -> None:
        for s in list(rec.steps.values()):
            if s.done:
                continue
            if not self.elector.still_leader(takeover):          # verify Lease ownership
                raise Fenced("lost lease")
            try:
                self.actions.apply(s)
            except Exception:
                log.exception("apply failed for %s, will retry next tick", s.key)
                return
            s.done = True
            self.store.put(rec)                                  # conditional write

    # -- 3. cluster must match the plan
    def _reconcile(self, rec: PlanRecord) -> None:
        for s in rec.steps.values():
            if s.done:
                try:
                    if not self.actions.verify(s):
                        log.warning("drift on %s, re-applying", s.key)
                        self.actions.apply(s)
                except Exception:
                    log.exception("reconcile failed for %s", s.key)
