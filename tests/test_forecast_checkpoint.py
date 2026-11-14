from lifeboat.checkpoint import FileCheckpointStore
from lifeboat.forecast import Holt, TrafficForecaster
from lifeboat.models import Level


def test_holt_follows_ramp():
    h = Holt(0.5, 0.3)
    for x in range(0, 100, 10):
        h.update(x)
    assert h.forecast(5) > 90


def test_no_phantom_vote_after_a_surge_ends():
    # Regression: when `make demo`'s surge stopped, the Holt level went below 0 and the trend
    # bounced back, so S3 voted L1/L2 for about 2 minutes with no traffic at all.
    from lifeboat.config import Config
    cfg = Config()
    f = TrafficForecaster(cfg.holt_alpha, cfg.holt_beta, cfg.forecast_horizon_steps,
                          cfg.x_factor, cfg.y_factor, cfg.forecast_warmup_samples)
    users = lambda t: 10 if t < 150 else 200 if t < 330 else 10 if t < 360 else 0  # noqa: E731
    rate = lambda t: sum(users(max(0, t - k)) for k in range(60)) / 60 * 0.7     # noqa: E731
    after = []
    for t in range(0, 900, 15):
        f.observe(rate(t))
        if t == 210:
            assert f.level() == Level.L2          # the surge itself is still caught
        if t >= 375:
            after.append(f.level())
    assert set(after) == {Level.L0}


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


def test_s3_client_has_short_timeouts(monkeypatch):
    import boto3

    from lifeboat.checkpoint import S3CheckpointStore
    seen = {}

    class FakeClient:
        def head_bucket(self, **_):
            return {}

    def fake_client(service, endpoint_url=None, config=None):
        seen["config"] = config
        return FakeClient()
    monkeypatch.setattr(boto3, "client", fake_client)
    S3CheckpointStore("bucket", "http://minio:9000")
    cfg = seen["config"]
    assert cfg.connect_timeout == 2 and cfg.read_timeout == 3
    assert cfg.retries["max_attempts"] == 2


def test_list_keys_and_lifecycle_events(tmp_path):
    s = FileCheckpointStore(str(tmp_path))
    s.save("c3", {"unit": 5})
    s.record_event("c3", "start", 0)
    s.record_event("c3", "term", 40)
    s.record_event("c4", "start", 0)
    assert s.list_keys("events/c3/") == sorted(s.list_keys("events/c3/"))
    assert len(s.list_keys("events/")) == 3 and len(s.list_keys("c3/")) == 2
    evs = s.events()
    assert [(e["name"], e["kind"], e["unit"]) for e in evs] == [("c3", "start", 0), ("c3", "term", 40),
                                                                ("c4", "start", 0)]
    assert set(evs[0]) == {"name", "kind", "unit", "ts"}


def test_e3_maths():
    from lifeboat.e3_collect import by_workload, parse_log, progress_lost_s, restore_s
    log = "\n".join([
        "starting...",
        'E3EVENT {"kind": "start", "name": "c3", "ts": 100.0, "unit": 0}',
        'E3EVENT {"kind": "term", "name": "c3", "ts": 200.0, "unit": 1000}',
        'E3EVENT {"kind": "start", "name": "c3", "ts": 500.0, "unit": 990}',     # 10 units redone
        'E3EVENT {"kind": "term", "name": "c3", "ts": 600.0, "unit": 2000}',
        'E3EVENT {"kind": "start", "name": "c3", "ts": 700.0, "unit": 1400}',    # 600 units redone
        'E3EVENT {"kind": "start", "name": "c4", "ts": 100.0, "unit": 0}',
    ])
    evs = by_workload(parse_log(log))
    assert progress_lost_s(evs["c3"], 0.05) == 30.5
    assert restore_s(evs["c3"], crisis_end_ts=430.0) == 270.0                  # last start after the end
    assert progress_lost_s(evs["c4"], 0.05) == 0 and restore_s(evs["c4"], 430.0) == 0.0
    stuck = evs["c3"][:2]                                                       # paused, never back
    assert restore_s(stuck, 430.0) is None
