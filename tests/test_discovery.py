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
