"""Controller tick logic with fakes (no cluster): takeover must not forget the crisis (F8)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from lifeboat import controller as ctl
from lifeboat import ingest
from lifeboat.config import Config
from lifeboat.executor import Executor
from lifeboat.forecast import TrafficForecaster
from lifeboat.models import Level, Reading, Step, step_key
from lifeboat.plan_store import InMemoryPlanStore
from lifeboat.signing import sign
from lifeboat.voter import Voter
from tests.fakes import FakeActions, FakeCluster, FakeElector, FakeLease

SECRET = "s"
PLAN = [Step(step_key("suspend_job", "ns", f"j{i}"), "suspend_job", "ns", f"j{i}", {}) for i in (1, 2, 3)]


def make_replica(store, cluster, elector, calm_s=300.0) -> ctl.Lifeboat:
    """A Lifeboat with fake Prometheus, Kubernetes and plan store (no __init__: it needs a cluster)."""
    lb = ctl.Lifeboat.__new__(ctl.Lifeboat)
    lb.cfg = Config(hmac_secret=SECRET, calm_minutes=calm_s / 60)
    lb.cp = None
    lb.monitor = NS(c1_struggling=lambda: False, cluster_rps_sample=lambda: None,
                    c1_rps_sample=lambda: None)
    lb.book = ingest.ReadingBook()
    lb.voter = Voter(SECRET, calm_s, lb.cfg.vote_window_s)
    lb.traffic = TrafficForecaster(0.3, 0.3, 12, 2.0, 4.0, 8)
    lb.c1_traffic = TrafficForecaster(0.3, 0.3, 12, 2.0, 4.0, 8)
    lb._last_c1_rps = None
    lb.store = store
    lb.executor = Executor(store, FakeActions(cluster), elector)
    lb._last_sample = 0.0
    return lb


@pytest.fixture(autouse=True)
def no_cluster(monkeypatch):
    monkeypatch.setattr(ctl.discovery, "list_workloads", lambda cfg, cp: [])
    monkeypatch.setattr(ctl.discovery, "free_millis", lambda: 0.0)
    # the plan for a level: the three pauses at L2 and above, nothing below
    monkeypatch.setattr(ctl, "make_plan", lambda level, *a, **k: list(PLAN) if level >= Level.L2 else [])


def test_takeover_keeps_the_crisis_level_and_the_plan():
    # bugs_repro section B as a test
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    A, B = make_replica(store, cluster, ea), make_replica(store, cluster, eb)
    ea.acquire_if_expired()
    now = 1000.0
    for src, ttl in (("S1", 150), ("S2", 330)):
        A.book.add(sign(Reading(src, 2, now, now + ttl), SECRET))
    A.step(now)
    assert store.get().level == 2 and len(store.get().steps) == 3

    # B restarted 20 s ago: empty reading book. As standby it follows the plan level.
    B.step(now + 5)
    assert B.voter.level == Level.L2

    eb.acquire_if_expired()                                   # A dies, B takes over
    for i in range(6):
        B.step(now + 20 + 5 * i)
        rec = store.get()
        assert rec.level == 2, f"tick {i}: level dropped to {rec.level}"
        assert len(rec.steps) == 3, f"tick {i}: a step was undone"


def test_new_leader_steps_down_only_after_its_own_calm_period():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    A, B = make_replica(store, cluster, ea, calm_s=60), make_replica(store, cluster, eb, calm_s=60)
    ea.acquire_if_expired()
    now = 1000.0
    for src in ("S1", "S2"):
        A.book.add(sign(Reading(src, 2, now, now + 30), SECRET))
    A.step(now)
    B.step(now + 1)
    eb.acquire_if_expired()
    t = now + 40                                              # readings expired, B now leader
    B.step(t)
    assert store.get().level == 2
    B.step(t + 59)
    assert store.get().level == 2                             # calm timer started at takeover
    B.step(t + 61)
    assert store.get().level == 1                             # one level per calm period
