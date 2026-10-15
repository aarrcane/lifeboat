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
        f.observe(10, Level.L0)
    assert f.level() == Level.L0
    for r in (20, 40, 80, 160):
        f.observe(r, Level.L1)
    assert f.level() == Level.L2


def test_checkpoint_roundtrip(tmp_path):
    s = FileCheckpointStore(str(tmp_path))
    assert s.latest_ts("job") is None and s.load("job") is None
    ts = s.save("job", {"unit": 42})
    assert s.latest_ts("job") == ts and s.load("job") == {"unit": 42}
    r = s.request("job")
    assert s.last_request_ts("job") == r
