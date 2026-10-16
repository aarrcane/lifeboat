import pytest

from lifeboat.executor import Executor
from lifeboat.models import Step, step_key
from lifeboat.plan_store import Fenced, InMemoryPlanStore
from tests.fakes import Crash, FakeActions, FakeCluster, FakeElector, FakeLease


def steps(n=4):
    return [Step(step_key("suspend_job", "ns", f"j{i}"), "suspend_job", "ns", f"j{i}", {})
            for i in range(1, n + 1)]


def test_crash_mid_plan_standby_finishes_and_old_is_fenced():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    ea.acquire_if_expired()                                            # A active, takeover 1
    A = Executor(store, FakeActions(cluster, crash_after_apply_of="suspend_job:ns/j2"), ea)
    B = Executor(store, FakeActions(cluster), eb)

    with pytest.raises(Crash):
        A.tick(2, steps())                                            # dies after step 2, before marking it
    rec = store.get()
    assert [s.done for s in rec.steps.values()] == [True, False, False, False]
    assert all(s.original for s in rec.steps.values())                # originals saved first

    assert B.tick(2, steps()) == "standby"                            # lease still held by A
    eb.acquire_if_expired()                                           # A's lease expired
    assert B.tick(2, steps()) == "active"
    rec = store.get()
    assert rec.takeover == 2 and all(s.done for s in rec.steps.values())
    assert cluster.apply_log.count("suspend_job:ns/j2") == 2          # redone, harmless

    stale = rec.__class__(level=2, takeover=1)                        # late write from old A
    with pytest.raises(Fenced):
        store.put(stale)


def test_old_executor_fenced_on_next_tick():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    ea.acquire_if_expired()
    A = Executor(store, FakeActions(cluster), ea)
    B = Executor(store, FakeActions(cluster), eb)
    A.tick(2, steps(2))
    eb.acquire_if_expired()
    B.tick(2, steps(2))
    assert A.tick(2, steps(2)) == "standby"                           # no longer holds lease


def test_revert_is_slow_and_checked_against_original():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea = FakeElector("A", lease); ea.acquire_if_expired()
    E = Executor(store, FakeActions(cluster), ea)
    E.tick(2, steps(3))
    E.tick(0, [])                                                     # crisis over
    assert len(store.get().steps) == 2                                # one step undone per tick
    E.tick(0, []); E.tick(0, [])
    assert store.get().steps == {}
    assert all(v == {"suspend": False, "replicas": 3, "data": {}} for v in cluster.state.values())


def test_drift_is_reconciled():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea = FakeElector("A", lease); ea.acquire_if_expired()
    E = Executor(store, FakeActions(cluster), ea)
    E.tick(2, steps(1))
    cluster.state["suspend_job:ns/j1"] = {"suspend": False}           # someone un-suspended it
    E.tick(2, steps(1))
    assert cluster.state["suspend_job:ns/j1"] == {"suspend": True}


def test_prepare_runs_once_for_all_pending_steps():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea = FakeElector("A", lease); ea.acquire_if_expired()
    E = Executor(store, FakeActions(cluster), ea)
    E.tick(2, steps(3))
    assert cluster.prepared == [[s.key for s in steps(3)]]           # one batched wait, not three
    E.tick(2, steps(3))
    assert len(cluster.prepared) == 1                                 # nothing pending => no wait


def test_lease_lost_during_prepare_applies_nothing():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    ea.acquire_if_expired()
    cluster.lose_lease_in_prepare = eb.acquire_if_expired              # A stalls, B takes over
    A = Executor(store, FakeActions(cluster), ea)
    assert A.tick(2, steps(2)) == "fenced"
    assert cluster.apply_log == []


def test_prepare_failure_does_not_block_pauses():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea = FakeElector("A", lease); ea.acquire_if_expired()

    class Boom(FakeActions):
        def prepare(self, steps):
            raise RuntimeError("checkpoint store down")
    E = Executor(store, Boom(cluster), ea)
    assert E.tick(2, steps(2)) == "active"
    assert all(s.done for s in store.get().steps.values())
