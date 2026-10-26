import json
from pathlib import Path

from lifeboat import ingest
from lifeboat.models import Level, Reading
from lifeboat.signing import sign
from lifeboat.voter import Voter

SECRET = "s"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TTL = {"S1": 150.0, "S2": 330.0}


def rd(src, lvl, now, ttl=120, secret=SECRET):
    return sign(Reading(src, lvl, now, now + ttl), secret)


def test_single_source_only_l1():
    v = Voter(SECRET, 300)
    assert v.evaluate([rd("S1", 2, 0)], 1, False) == Level.L1


def test_single_noisy_source_cannot_vote_twice():
    v = Voter(SECRET, 300)
    # Two L2 readings from the SAME source S1 must still only count as 1 source (L1)
    assert v.evaluate([rd("S1", 2, 0), rd("S1", 2, 5)], 6, False) == Level.L1


def test_two_sources_l2():
    v = Voter(SECRET, 300)
    assert v.evaluate([rd("S1", 2, 0), rd("S2", 2, 0)], 1, False) == Level.L2


def test_naive_voter_triggers_l2_on_single_source():
    naive = Voter(SECRET, 0, naive=True)
    assert naive.evaluate([rd("S1", 2, 0)], 1, False) == Level.L2
    assert naive.evaluate([], 2, False) == Level.L0


def test_forged_or_expired_ignored():
    v = Voter(SECRET, 300)
    forged = rd("S2", 2, 0, secret="wrong")
    expired = rd("S1", 2, 0, ttl=1)
    assert v.evaluate([forged, expired, rd("S3", 2, 0)], 50, False) == Level.L1


def test_outside_vote_window_ignored():
    v = Voter(SECRET, 300, window_s=600)
    old_but_long_ttl = rd("S1", 2, 0, ttl=1000)
    assert v.evaluate([old_but_long_ttl, rd("S2", 2, 700)], 700, False) == Level.L1


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


def test_ingest_readings_and_override():
    book = ingest.ReadingBook()
    voter = Voter(SECRET, 300)
    overrides = []

    def post(path: str, payload: dict | bytes, headers: dict | None = None, now: float = 1000.0) -> int:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        code, _ = ingest.handle_post(
            path, raw, headers or {}, SECRET, book,
            lambda lvl, who: (overrides.append((lvl, who)), voter.set_override(lvl)),
            now=now,
        )
        return code

    now = 1000.0
    # 1. Valid S1 L2 -> 202, voter -> L1
    assert post("/readings", rd("S1", 2, now).to_dict(), now=now) == 202
    assert voter.evaluate(book.snapshot(), now, False) == Level.L1

    # 2. Forged signature -> 401, voter stays L1
    assert post("/readings", rd("S2", 2, now, secret="bad").to_dict(), now=now) == 401
    assert voter.evaluate(book.snapshot(), now, False) == Level.L1

    # 3. Expired reading -> 422, voter stays L1
    assert post("/readings", rd("S2", 2, now - 200, ttl=10).to_dict(), now=now) == 422
    assert voter.evaluate(book.snapshot(), now, False) == Level.L1

    # 4. Malformed payload -> 400
    assert post("/readings", b"{not-json", now=now) == 400

    # 5. Second valid source S2 L2 -> 202, voter -> L2
    assert post("/readings", rd("S2", 2, now).to_dict(), now=now) == 202
    assert voter.evaluate(book.snapshot(), now, False) == Level.L2

    # 6. Override without token -> 401; with valid token -> 200
    assert post("/override", {"level": 3, "who": "op"}, now=now) == 401
    assert post("/override", {"level": 3, "who": "op"}, {"X-Lifeboat-Token": SECRET}, now=now) == 200
    assert voter.evaluate(book.snapshot(), now, False) == Level.L3
    assert overrides == [(Level.L3, "op")]


def _replay_peak(filename: str) -> Level:
    events = [json.loads(line) for line in (DATA_DIR / filename).read_text(encoding="utf-8").splitlines() if line]
    by_t: dict[int, list[dict]] = {}
    for e in events:
        by_t.setdefault(int(e["t"]), []).append(e)
    book = ingest.ReadingBook()
    voter = Voter(SECRET, calm_seconds=300.0)
    peak = Level.L0
    for t in range(0, 1800, 30):
        for e in by_t.get(t, []):
            src = e["source"]
            book.add(rd(src, int(e["level"]), float(t), ttl=TTL[src]))
        lvl = voter.evaluate(book.snapshot(), float(t), False)
        if lvl > peak:
            peak = lvl
    return peak


def test_e4_replay_scenarios_zero_false_l2_and_zero_missed():
    assert _replay_peak("scenario_flash_flood.jsonl") == Level.L2
    assert _replay_peak("scenario_noisy.jsonl") == Level.L1
    assert _replay_peak("scenario_conflicting.jsonl") == Level.L1
    assert _replay_peak("scenario_no_crisis.jsonl") == Level.L1
