import json
import os
from pathlib import Path
import threading
import urllib.request
from unittest.mock import patch

from feeds import common, handler, nea, pub
from feeds.nea import level_from_payload as nea_level, max_rain
from feeds.pub import count_alerts, level_from_payload as pub_level
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
    assert pub_level({"data": {"records": []}}) == 0
    # Cancelled CAP message should report 0 active alerts
    cancelled = {
        "code": 0,
        "data": {
            "records": [{
                "datetime": "2026-10-26T15:00:00+08:00",
                "item": {"type": "observation", "msgType": "Cancel", "readings": [{"area": "Bukit Timah"}]},
            }]
        },
    }
    assert count_alerts(cancelled) == 0
    assert pub_level(cancelled) == 0


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
    assert count_alerts(_load("pub_flood_alert.json")) == 2
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


def test_openfaas_secret_file_and_handler_server(tmp_path):
    secret_file = tmp_path / "LIFEBOAT_HMAC_SECRET"
    secret_file.write_text("openfaas-mounted-key\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LIFEBOAT_HMAC_SECRET", None)
        assert common.load_secret((secret_file,)) == "openfaas-mounted-key"

    with patch.object(common, "get_json", return_value=_load("nea_heavy_storm.json")), \
         patch.object(common, "send", return_value=[202, 202]), \
         patch.dict(os.environ, {"FEED_SOURCE": "S2"}):
        assert handler.handle() == {"source": "S2", "level": 2, "sent": [202, 202]}
        assert handler.handle_http("GET", "/_/health") == (200, {"ok": True, "source": "S2"})
        assert handler.handle_http("POST", "/", b'{"source": "S2"}') == (
            200,
            {"source": "S2", "level": 2, "sent": [202, 202]},
        )

    with patch.object(common, "get_json", return_value=_load("pub_flood_alert.json")), \
         patch.object(common, "send", return_value=[202, 202]), \
         patch.dict(os.environ, {"FEED_SOURCE": "S1"}):
        assert handler.handle_http("POST", "/") == (
            200,
            {"source": "S1", "level": 2, "sent": [202, 202]},
        )


def test_cronjob_run_once_exit_codes():
    with patch.object(common, "get_json", return_value=_load("pub_flood_alert.json")), \
         patch.dict(os.environ, {"FEED_SOURCE": "S1"}):
        with patch.object(common, "send", return_value=[202, 202]):
            assert handler.run_once() == 0
        with patch.object(common, "send", return_value=[401, 0]):
            assert handler.run_once() == 1  # no replica accepted the reading
    with patch.object(common, "get_json", side_effect=OSError("data.gov.sg down")), \
         patch.dict(os.environ, {"FEED_SOURCE": "S2"}):
        assert handler.run_once() == 1  # nothing sent; the old reading expires by its TTL
