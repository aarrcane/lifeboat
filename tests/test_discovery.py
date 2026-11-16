from types import SimpleNamespace as NS

from lifeboat.discovery import is_unschedulable


def pod(*conditions):
    return NS(status=NS(conditions=[NS(type=t, status=s, reason=r) for t, s, r in conditions]))


def test_starting_pod_is_not_stuck():
    # scheduled, still pulling the image / starting: phase is Pending but there is room
    assert not is_unschedulable(pod(("PodScheduled", "True", None),
                                    ("Ready", "False", "ContainersNotReady")))
    assert not is_unschedulable(pod())
    assert not is_unschedulable(NS(status=None))


def test_pod_waiting_for_room_is_stuck():
    assert is_unschedulable(pod(("PodScheduled", "False", "Unschedulable")))


# ---- list_workloads: one bad object must not stop the tick (F10, F11)
import pytest  # noqa: E402

from lifeboat import discovery  # noqa: E402
from lifeboat.config import Config  # noqa: E402
from lifeboat.models import Tier  # noqa: E402


def _template(cpu):
    return NS(spec=NS(containers=[NS(resources=NS(requests={"cpu": cpu}))]))


def _job(name, tier, status=None):
    return NS(metadata=NS(name=name, namespace="lifeboat-demo", labels={"lifeboat.io/tier": tier},
                          annotations={}),
              spec=NS(template=_template("500m"), parallelism=1), status=status)


def _deploy(name, tier):
    return NS(metadata=NS(name=name, namespace="lifeboat-demo", labels={"lifeboat.io/tier": tier},
                          annotations={}),
              spec=NS(template=_template("250m"), replicas=2))


@pytest.fixture
def fake_api(monkeypatch):
    objs = {"jobs": [], "deploys": []}
    monkeypatch.setattr(discovery.client, "BatchV1Api",
                        lambda: NS(list_namespaced_job=lambda ns, label_selector, **kw: NS(items=objs["jobs"])))
    monkeypatch.setattr(discovery.client, "AppsV1Api",
                        lambda: NS(list_namespaced_deployment=lambda ns, label_selector, **kw: NS(items=objs["deploys"])))
    return objs


class _CP:
    def __init__(self, fail=False):
        self.fail = fail

    def latest_ts(self, name):
        if self.fail:
            raise TimeoutError("S3 read timed out")
        return 123.0


def test_finished_jobs_are_not_capacity(fake_api):
    done = NS(completion_time="2026-11-14T00:00:00Z", conditions=[])
    failed = NS(completion_time=None, conditions=[NS(type="Failed", status="True")])
    running = NS(completion_time=None, conditions=[NS(type="Suspended", status="True")])
    fake_api["jobs"] = [_job("done", "C3", done), _job("failed", "C4", failed),
                        _job("running", "C3", running), _job("new", "C4", None)]
    names = [w.name for w in discovery.list_workloads(Config(), _CP())]
    assert names == ["running", "new"]


def test_unknown_tier_label_is_skipped(fake_api):
    fake_api["jobs"] = [_job("typo", "C9"), _job("ok", "C3")]
    fake_api["deploys"] = [_deploy("c1-emergency", "C1"), _deploy("bad", "gold")]
    ws = discovery.list_workloads(Config(), _CP())
    assert [(w.name, w.tier) for w in ws] == [("ok", Tier.C3), ("c1-emergency", Tier.C1)]


def test_checkpoint_store_failure_means_no_checkpoint_known(fake_api):
    fake_api["jobs"] = [_job("c3-analytics", "C3")]
    ws = discovery.list_workloads(Config(), _CP(fail=True))
    assert ws[0].last_checkpoint_ts is None
    assert discovery.list_workloads(Config(), _CP())[0].last_checkpoint_ts == 123.0


def test_unschedulable_only_counts_after_grace():
    from datetime import datetime, timezone
    t0 = datetime(2026, 11, 14, tzinfo=timezone.utc)
    p = NS(status=NS(conditions=[NS(type="PodScheduled", status="False", reason="Unschedulable",
                                    last_transition_time=t0)]))
    assert not is_unschedulable(p, 20, now=t0.timestamp() + 5)    # waiting for paused pods to go
    assert is_unschedulable(p, 20, now=t0.timestamp() + 30)
    assert is_unschedulable(p, now=t0.timestamp() + 5)            # no grace asked: stuck at once
