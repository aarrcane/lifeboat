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


def c1_step(replicas=10):
    return Step(step_key("scale_deployment", "ns", "c1"), "scale_deployment", "ns", "c1",
                {"replicas": replicas})


def active_executor():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    ea = FakeElector("A", lease); ea.acquire_if_expired()
    return store, cluster, Executor(store, FakeActions(cluster), ea)


def test_failing_step_does_not_block_later_steps():
    # bugs_repro section C: a pause that fails must not stop C1 being scaled
    store, cluster, E = active_executor()
    E.tick(2, steps(1))                                               # j1 in the plan, done
    cluster.state.clear()
    store_rec = store.get(); store_rec.steps["suspend_job:ns/j1"].done = False; store.put(store_rec)
    cluster.broken.add("suspend_job:ns/j1")                          # its API calls now fail
    for _ in range(3):
        assert E.tick(2, steps(3) + [c1_step()]) == "active"
    done = {s.name: s.done for s in store.get().steps.values()}
    assert done == {"j1": False, "j2": True, "j3": True, "c1": True}
    assert cluster.state[c1_step().key] == {"replicas": 10}
    cluster.broken.clear()
    E.tick(2, steps(3) + [c1_step()])                                 # retried next tick
    assert all(s.done for s in store.get().steps.values())


def test_deleted_object_is_dropped_from_plan_and_others_go_ahead():
    store, cluster, E = active_executor()
    E.tick(2, steps(2))
    rec = store.get(); rec.steps["suspend_job:ns/j1"].done = False; store.put(rec)
    cluster.gone.add("suspend_job:ns/j1")                             # the Job was deleted
    E.tick(2, steps(2) + [c1_step()])
    rec = store.get()
    assert "suspend_job:ns/j1" not in rec.steps
    assert rec.steps[c1_step().key].done


def test_new_step_for_deleted_object_is_not_added():
    store, cluster, E = active_executor()
    cluster.gone.add("suspend_job:ns/j2")
    assert E.tick(2, steps(3)) == "active"
    assert list(store.get().steps) == ["suspend_job:ns/j1", "suspend_job:ns/j3"]


def test_failed_revert_does_not_block_older_reverts():
    # bugs_repro section D: newest step cannot be reverted, the older ones still are
    store, cluster, E = active_executor()
    E.tick(2, steps(3))
    cluster.broken.add("suspend_job:ns/j3")
    for _ in range(10):
        E.tick(0, [])
    assert list(store.get().steps) == ["suspend_job:ns/j3"]           # only the broken one waits
    assert cluster.state["suspend_job:ns/j1"] == {"suspend": False, "replicas": 3, "data": {}}
    cluster.broken.clear()
    E.tick(0, [])
    assert store.get().steps == {}


def test_revert_of_deleted_object_is_dropped():
    store, cluster, E = active_executor()
    E.tick(2, steps(3))
    cluster.gone.add("suspend_job:ns/j3")
    E.tick(0, [])                                                     # j3 dropped, j2 reverted, same tick
    assert list(store.get().steps) == ["suspend_job:ns/j1"]
