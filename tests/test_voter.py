from lifeboat.models import Level, Reading
from lifeboat.signing import sign
from lifeboat.voter import Voter

SECRET = "s"


def rd(src, lvl, now, ttl=120, secret=SECRET):
    return sign(Reading(src, lvl, now, now + ttl), secret)


def test_single_source_only_l1():
    v = Voter(SECRET, 300)
    assert v.evaluate([rd("S1", 2, 0)], 1, False) == Level.L1


def test_two_sources_l2():
    v = Voter(SECRET, 300)
    assert v.evaluate([rd("S1", 2, 0), rd("S2", 2, 0)], 1, False) == Level.L2


def test_forged_or_expired_ignored():
    v = Voter(SECRET, 300)
    forged = rd("S2", 2, 0, secret="wrong")
    expired = rd("S1", 2, 0, ttl=1)
    assert v.evaluate([forged, expired, rd("S3", 2, 0)], 50, False) == Level.L1


def test_c1_struggling_skips_vote():
    v = Voter(SECRET, 300)
    assert v.evaluate([], 0, True) == Level.L3


def test_step_down_needs_calm_and_goes_one_level():
    v = Voter(SECRET, 300)
    v.evaluate([], 0, True)                       # L3
    assert v.evaluate([], 10, False) == Level.L3  # calm timer starts
    assert v.evaluate([], 200, False) == Level.L3
    assert v.evaluate([], 311, False) == Level.L2
    assert v.evaluate([], 400, False) == Level.L2
    assert v.evaluate([], 620, False) == Level.L1


def test_override_locks_level():
    v = Voter(SECRET, 300)
    v.set_override(Level.L2)
    assert v.evaluate([], 0, False) == Level.L2
    v.set_override(None)
    v.evaluate([], 1, False)
    assert v.level == Level.L2        # still held until calm period passes
