from feeds.nea import level_from_payload as nea
from feeds.pub import level_from_payload as pub


def test_nea_levels():
    mk = lambda v: {"data": {"readings": [{"data": [{"stationId": "S1", "value": v}]}]}}  # noqa: E731
    assert (nea(mk(0)), nea(mk(3)), nea(mk(9))) == (0, 1, 2)


def test_pub_levels():
    assert pub({"items": []}) == 0
    assert pub({"data": {"flood_alerts": [{"x": 1}]}}) == 2
