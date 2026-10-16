from lifeboat.checkpoint import FileCheckpointStore
from lifeboat.forecast import Holt, TrafficForecaster
from lifeboat.models import Level


def test_holt_follows_ramp():
    h = Holt(0.5, 0.3)
    for x in range(0, 100, 10):
        h.update(x)
    assert h.forecast(5) > 90


def test_traffic_level_thresholds():
    f = TrafficForecaster(0.5, 0.3, 4, x=2, y=4)
    for _ in range(30):
        f.observe(10)
    assert f.level() == Level.L0
    for r in (20, 40, 80, 160):
        f.observe(r)
    assert f.level() == Level.L2


def test_steady_traffic_never_votes_and_baseline_learns():
    # Regression: the baseline started at 1 rps and only learned at voter level L0, so
    # normal traffic of 10 rps voted L2 forever once that vote had raised the level to L1.
    f = TrafficForecaster(0.5, 0.3, 12, x=2, y=4, warmup=8)
    levels = []
    for _ in range(200):
        f.observe(10)
        levels.append(f.level())
    assert set(levels) == {Level.L0}
    assert abs(f.baseline - 10) < 0.5


def test_no_vote_during_warmup():
    f = TrafficForecaster(0.5, 0.3, 4, x=2, y=4, warmup=8)
    for r in (10, 50, 200):
        f.observe(r)
        assert f.level() == Level.L0


def test_baseline_does_not_learn_from_a_surge():
    f = TrafficForecaster(0.5, 0.3, 4, x=2, y=4, warmup=8)
    for _ in range(20):
        f.observe(10)
    for _ in range(100):
        f.observe(200)                                     # long surge
    assert f.level() == Level.L2 and f.baseline < 15


def test_checkpoint_roundtrip(tmp_path):
    s = FileCheckpointStore(str(tmp_path))
    assert s.latest_ts("job") is None and s.load("job") is None
    ts = s.save("job", {"unit": 42})
    assert s.latest_ts("job") == ts and s.load("job") == {"unit": 42}
    r = s.request("job")
    assert s.last_request_ts("job") == r
