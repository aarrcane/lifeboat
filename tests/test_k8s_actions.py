import time

from lifeboat.checkpoint import FileCheckpointStore
from lifeboat.config import Config
from lifeboat.k8s_actions import K8sActions
from lifeboat.models import Step, step_key


def job(name):
    return Step(step_key("suspend_job", "ns", name), "suspend_job", "ns", name, {"checkpoint": True})


def actions(tmp_path, **cfg):
    a = K8sActions(Config(**cfg), FileCheckpointStore(str(tmp_path)))
    a._current = lambda s: {"suspend": False}                     # every job still running
    return a


def test_wait_is_bounded_by_half_the_lease(tmp_path):
    a = actions(tmp_path, checkpoint_wait_s=60, lease_duration_s=4)
    t0 = time.time()
    a.prepare([job("j1"), job("j2"), job("j3")])                  # nobody answers
    assert time.time() - t0 < 3                                   # one wait of <= 2 s, not 3 x 60 s


def test_returns_as_soon_as_all_apps_saved(tmp_path):
    a = actions(tmp_path, checkpoint_wait_s=5)
    orig_request = a.cp.request

    def request_and_answer(name):                                  # app saves right away
        ts = orig_request(name)
        a.cp.save(name, {"unit": 1})
        return ts
    a.cp.request = request_and_answer
    t0 = time.time()
    a.prepare([job("j1"), job("j2")])
    assert time.time() - t0 < 1
