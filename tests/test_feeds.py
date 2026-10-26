import json
from pathlib import Path
from unittest.mock import patch

from feeds import common, nea, pub
from feeds.nea import level_from_payload as nea_level, max_rain
from feeds.pub import level_from_payload as pub_level
from lifeboat.signing import verify

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_nea_levels():
    mk = lambda v: {"data": {"readings": [{"data": [{"stationId": "S1", "value": v}]}]}}  # noqa: E731
    assert (nea_level(mk(0)), nea_level(mk(3)), nea_level(mk(9))) == (0, 1, 2)


def test_pub_levels():
    assert pub_level({"items": []}) == 0
    assert pub_level({"data": {"flood_alerts": [{"x": 1}]}}) == 2


def test_nea_real_fixtures_and_v1_shape():
    assert nea_level(_load("nea_clear.json")) == 0
    assert nea_level(_load("nea_moderate_rain.json")) == 1
    assert nea_level(_load("nea_heavy_storm.json")) == 2
    assert max_rain(_load("nea_heavy_storm.json")) == 11.4
    # v1 environment/rainfall fallback shape
    v1_payload = {"items": [{"readings": [{"station_id": "S77", "value": 8.0}]}]}
    assert nea_level(v1_payload) == 2


def test_pub_real_fixtures():
    assert pub_level(_load("pub_clear.json")) == 0
    assert pub_level(_load("pub_flood_alert.json")) == 2


def test_handlers_sign_and_send():
    with patch.object(common, "get_json", return_value=_load("pub_flood_alert.json")), \
         patch.object(common, "send", return_value=[202, 202]) as mock_send:
        res = pub.handle()
        assert res == {"source": "S1", "level": 2, "sent": [202, 202]}
        sent_reading = mock_send.call_args[0][0]
        assert verify(sent_reading, common.SECRET)

    with patch.object(common, "get_json", return_value=_load("nea_moderate_rain.json")), \
         patch.object(common, "send", return_value=[202, 202]) as mock_send:
        res = nea.handle()
        assert res == {"source": "S2", "level": 1, "sent": [202, 202]}
        sent_reading = mock_send.call_args[0][0]
        assert verify(sent_reading, common.SECRET)
