"""Regression tests for the fixes from the 16 Nov audit (B1-B9, R1, R2, R7)."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from feeds import common, handler, nea, pub, usgs_quake
from lifeboat import discovery, ingest
from lifeboat.config import Config
from lifeboat.executor import Executor
from lifeboat.k8s_actions import K8sActions
from lifeboat.models import Level, PlanRecord, Reading, Step, step_key
from lifeboat.override_store import InMemoryOverrideStore, Override
from lifeboat.plan_store import Gone, InMemoryPlanStore
from lifeboat.signing import sign
from lifeboat.voter import Voter
from tests import test_controller_logic as tcl
from tests.fakes import FakeActions, FakeCluster, FakeElector, FakeLease

SECRET = tcl.SECRET
REAL_LIST_WORKLOADS = discovery.list_workloads


@pytest.fixture(autouse=True)
def no_cluster(monkeypatch):
    monkeypatch.setattr(tcl.ctl.discovery, "list_workloads", lambda cfg, cp: [])
    monkeypatch.setattr(tcl.ctl.discovery, "free_millis", lambda *a: 0.0)
    monkeypatch.setattr(tcl.ctl, "make_plan",
                        lambda level, *a, **k: list(tcl.PLAN) if level >= Level.L2 else [])


def post(path, body, book=None, headers=None, now=1000.0, on_override=lambda lvl, who: None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return ingest.handle_post(path, raw, headers or {}, SECRET, book or ingest.ReadingBook(),
                              on_override, now=now)[0]


# ---- B1: a monitoring outage must not keep an old forecast vote alive
def test_b1_no_forecast_vote_without_fresh_samples():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    el = FakeElector("A", lease)
    lb = tcl.make_replica(store, cluster, el)
    el.acquire_if_expired()
    samples = iter([10.0] * 12 + [200.0] * 6)
    lb.monitor = NS(c1_struggling=lambda: False, cluster_rps_sample=lambda: next(samples, None),
                    c1_rps_sample=lambda: None)
    t = 1000.0
    for _ in range(18):                                   # 3 min normal, 90 s surge
        t += 15
        lb.step(t)
    s3 = lambda: [r for r in lb.voter.valid_readings(lb.book.snapshot(), t) if r.source == "S3"]  # noqa: E731
    assert s3() and s3()[0].level == Level.L2             # the surge itself is a valid vote
    for _ in range(8):                                    # 2 min of Prometheus down
        t += 15
        lb.step(t)
    assert s3() == []                                     # the old L2 expired: S3 is silent


# ---- B2: one override for every replica, kept across a takeover
def test_b2_override_reaches_standby_and_survives_takeover():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    shared = InMemoryOverrideStore()
    ea, eb = FakeElector("A", lease), FakeElector("B", lease)
    A = tcl.make_replica(store, cluster, ea, overrides=shared)
    B = tcl.make_replica(store, cluster, eb, overrides=shared)
    ea.acquire_if_expired()
    now = 1000.0
    B._override(Level.L3, "operator")                     # the request landed on the standby
    A.step(now)
    assert store.get().level == Level.L3                  # ... and the leader acts on it
    eb.acquire_if_expired()                               # A dies
    for i in range(4):
        B.step(now + 20 + 5 * i)
    assert store.get().level == Level.L3                  # the new leader keeps it
    A._override(None, "operator")                         # cleared through the other pod
    B.step(now + 60)
    assert B.voter.override is None


def test_b2_override_store_roundtrip():
    ov = Override(Level.L2, "op", 12.5)
    assert Override.from_data(ov.to_data()) == ov
    assert Override.from_data({"level": ""}).level is None
    assert Override.from_data(None).level is None


def test_b2_override_not_stored_returns_503():
    def fail(lvl, who):
        raise RuntimeError("API down")
    assert post("/override", {"level": 2}, headers={"X-Lifeboat-Token": SECRET}, on_override=fail) == 503


# ---- B3: invalid override levels are a 400, not a crashed handler
@pytest.mark.parametrize("level", [7, -1, "abc", True, [1]])
def test_b3_bad_override_level_is_400(level):
    assert post("/override", {"level": level}, headers={"X-Lifeboat-Token": SECRET}) == 400


def test_b3_override_bad_token_401():
    assert post("/override", {"level": 2}, headers={"X-Lifeboat-Token": "wrong"}) == 401


# ---- B4: future-dated or out-of-range readings are refused; expired ones never shadow a source
def test_b4_future_dated_reading_rejected():
    book, now = ingest.ReadingBook(), 1000.0
    skewed = sign(Reading("S1", 0, now + 3600, now + 3750), SECRET)
    assert post("/readings", skewed.to_dict(), book, now=now) == 422
    good = sign(Reading("S1", 2, now + 120, now + 270), SECRET)
    assert post("/readings", good.to_dict(), book, now=now + 120) == 202
    book.add(sign(Reading("S2", 2, now + 120, now + 450), SECRET), now + 120)
    assert Voter(SECRET, 300).evaluate(book.snapshot(), now + 121, False) == Level.L2


def test_b4_too_long_lifetime_and_bad_level_rejected():
    now = 1000.0
    assert post("/readings", sign(Reading("S1", 2, now, now + 86400), SECRET).to_dict(), now=now) == 422
    assert post("/readings", sign(Reading("S1", 99, now, now + 100), SECRET).to_dict(), now=now) == 400
    assert post("/readings", sign(Reading("S1", -5, now, now + 100), SECRET).to_dict(), now=now) == 400


def test_b4_expired_reading_is_replaced_even_if_newer():
    book = ingest.ReadingBook()
    book.add(Reading("S1", 0, 5000.0, 5100.0), now=1000.0)  # wrong clock, already in the book
    book.add(Reading("S1", 2, 1200.0, 1350.0), now=5200.0)  # after it expired: replaces it
    assert [r.level for r in book.snapshot()] == [2]


def test_b4_voter_ignores_future_readings():
    now = 1000.0
    r = sign(Reading("S1", 2, now + 600, now + 700), SECRET)
    assert Voter(SECRET, 300).valid_readings([r], now) == []


# ---- B5: one bad annotation must not stop the tick
def _job(name, annotations):
    tmpl = NS(spec=NS(containers=[NS(resources=NS(requests={"cpu": "500m"}))]))
    return NS(metadata=NS(name=name, namespace="lifeboat-demo", labels={"lifeboat.io/tier": "C4"},
                          annotations=annotations),
              spec=NS(template=tmpl, parallelism=1), status=None)


def test_b5_bad_restart_annotation_falls_back(monkeypatch):
    jobs = [_job("bad", {"lifeboat.io/restart-seconds": "20s"}),
            _job("good", {"lifeboat.io/restart-seconds": "10"})]
    monkeypatch.setattr(discovery.client, "BatchV1Api",
                        lambda: NS(list_namespaced_job=lambda ns, label_selector, **kw: NS(items=jobs)))
    monkeypatch.setattr(discovery.client, "AppsV1Api",
                        lambda: NS(list_namespaced_deployment=lambda ns, label_selector, **kw: NS(items=[])))
    cp = NS(latest_ts=lambda name: None)
    ws = {w.name: w.restart_seconds for w in REAL_LIST_WORKLOADS(Config(), cp)}
    assert ws == {"bad": 30.0, "good": 10.0}


def test_r1_checkpoint_lookups_are_cached():
    calls = []

    class CP:
        def latest_ts(self, name):
            calls.append(name)
            return 42.0
    cp = CP()
    for t in (0.0, 10.0, 20.0):
        assert discovery._latest_checkpoint(cp, "c3", cache_s=30, now=1000 + t) == 42.0
    assert calls == ["c3"]
    assert discovery._latest_checkpoint(cp, "c3", cache_s=30, now=1031) == 42.0
    assert calls == ["c3", "c3"]


# ---- B6: the experiment runner puts the defaults back
def test_b6_finish_restores_calm_period_and_warmup():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_experiment as rx
    seen = []
    with patch.object(rx, "kubectl", side_effect=lambda *a, **k: seen.append(a) or ""):
        rx.finish()
    flat = [" ".join(a) for a in seen]
    assert any("LIFEBOAT_CALM_MINUTES-" in c and "LIFEBOAT_FORECAST_WARMUP_SAMPLES-" in c for c in flat)


# ---- B7: any registered feed can run, unknown ones fail loudly
def test_b7_quake_feed_is_reachable():
    with patch.dict(os.environ, {"FEED_SOURCE": "S4"}):
        assert handler.resolve_source() == "S4"
        with patch.object(usgs_quake, "handle", return_value={"source": "S4"}) as h:
            assert handler.handle() == {"source": "S4"}
            h.assert_called_once()
    with patch.dict(os.environ, {"FEED_SOURCE": "S9"}):
        with pytest.raises(ValueError):
            handler.handle()
        assert handler.run_once() == 1
    with patch.dict(os.environ, {"FEED_SOURCE": "nea"}):
        assert handler.resolve_source() == "S2"


# ---- B8: a deleted object does not cancel the checkpoint request for the others
def test_b8_prepare_continues_after_a_gone_step(tmp_path):
    from lifeboat.checkpoint import FileCheckpointStore
    cp = FileCheckpointStore(str(tmp_path))
    a = K8sActions(Config(checkpoint_wait_s=0.1), cp)
    gone = Step(step_key("suspend_job", "ns", "gone"), "suspend_job", "ns", "gone", {"checkpoint": True})
    ok = Step(step_key("suspend_job", "ns", "ok"), "suspend_job", "ns", "ok", {"checkpoint": True})

    def current(s):
        if s.name == "gone":
            raise Gone(s.key)
        return {"suspend": False}
    a._current = current
    a.prepare([gone, ok])
    assert cp.last_request_ts("ok") > 0 and cp.last_request_ts("gone") == 0.0


# ---- B9: exactly one undo per tick, even when the undo is not confirmed yet
def test_b9_one_revert_per_tick_even_unconfirmed():
    store, cluster, lease = InMemoryPlanStore(), FakeCluster(), FakeLease()
    el = FakeElector("A", lease)
    el.acquire_if_expired()

    class Slow(FakeActions):                           # the API accepted the revert, not visible yet
        def is_reverted(self, s):
            return False
    ex = Executor(store, Slow(cluster), el)
    steps = [Step(step_key("suspend_job", "ns", f"j{i}"), "suspend_job", "ns", f"j{i}") for i in range(3)]
    ex.tick(2, steps)
    reverted = []
    ex.actions.revert = lambda s: reverted.append(s.key)
    ex.tick(0, [])
    assert len(reverted) == 1


# ---- R2: /healthz follows the control loop
def test_r2_health_follows_the_loop():
    lb = tcl.make_replica(InMemoryPlanStore(), FakeCluster(), FakeElector("A", FakeLease()))
    lb.cfg = Config(loop_stall_s=60)
    import time
    lb._last_loop = time.time()
    assert lb.healthy()
    lb._last_loop = time.time() - 120
    assert not lb.healthy()


# ---- R7: feeds refuse payloads they cannot score instead of reporting "all clear"
@pytest.mark.parametrize("payload", [
    {"code": 0, "data": {}, "errorMsg": ""},
    {"code": 17, "data": {"records": []}, "errorMsg": "rate limited"},
    {"message": "Internal error"},
    ["not", "a", "dict"],
])
def test_r7_bad_payload_sends_nothing(payload):
    with patch.object(common, "get_json", return_value=payload), \
         patch.object(common, "send", return_value=[202]) as send:
        for feed in (pub, nea):
            with pytest.raises(common.BadPayload):
                feed.handle()
        send.assert_not_called()


def test_r7_v1_shape_still_accepted():
    assert common.check_datagov({"items": []}, "readings") == {"items": []}


def test_r1_prometheus_query_uses_short_timeout():
    from lifeboat.monitor import Monitor
    seen = {}

    def fake_get(url, params, timeout):
        seen["timeout"] = timeout
        raise OSError("down")
    with patch("lifeboat.monitor.requests.get", fake_get):
        assert Monitor(Config()).query("up") is None
    assert seen["timeout"] == 1.0


def test_plan_record_unchanged_by_override_feature():
    rec = PlanRecord(level=2, takeover=3)
    assert PlanRecord.from_spec(rec.to_spec()).to_spec() == rec.to_spec()
