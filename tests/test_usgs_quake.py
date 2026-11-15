from feeds import usgs_quake as q
from lifeboat.models import Level
from lifeboat.signing import sign
from lifeboat.models import Reading
from lifeboat.voter import Voter


def quake(mag, lat, lon, tsunami=0):
    return {"properties": {"mag": mag, "tsunami": tsunami},
            "geometry": {"coordinates": [lon, lat, 10.0]}}


def test_levels_by_magnitude_and_distance():
    near_lat, near_lon = 2.0, 104.0            # about 75 km from Singapore
    assert q.level_from_payload({"features": []}) == 0
    assert q.level_from_payload({"features": [quake(4.0, near_lat, near_lon)]}) == 0
    assert q.level_from_payload({"features": [quake(5.2, near_lat, near_lon)]}) == 1
    assert q.level_from_payload({"features": [quake(7.0, near_lat, near_lon)]}) == 2
    assert q.level_from_payload({"features": [quake(5.5, near_lat, near_lon, tsunami=1)]}) == 2
    assert q.level_from_payload({"features": [quake(8.0, 35.7, 139.7)]}) == 0   # Tokyo: too far
    assert q.level_from_payload({"features": [{"bad": 1}], "x": 0}) == 0
    assert q.level_from_payload(None) == 0


def test_voter_counts_a_new_source_like_any_other():
    v = Voter("k", calm_seconds=300)
    now = 1000.0
    rs = [sign(Reading("S2", 2, now, now + 300), "k"), sign(Reading("S4", 2, now, now + 300), "k")]
    assert v.target(rs, now, c1_struggling=False) == Level.L2
    assert v.target(rs[1:], now, c1_struggling=False) == Level.L1     # one source alone: L1 only
