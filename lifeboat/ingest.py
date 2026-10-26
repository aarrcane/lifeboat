"""HTTP ingest for signed readings + human override. Runs on BOTH replicas
(feeds POST to every pod via the headless Service) so both can run voter+planner.

Endpoints:
  GET  /healthz   -> 200 {"ok": true}
  POST /readings  -> body: {"source", "level", "ts", "expires_at", "sig"}
                     202 Accepted if valid HMAC and not expired
                     400 Bad Request if malformed JSON/fields (increments reason="malformed")
                     401 Unauthorized if HMAC fails (increments reason="bad_signature")
                     422 Unprocessable if already expired (increments reason="expired")
  POST /override  -> header: X-Lifeboat-Token: <LIFEBOAT_HMAC_SECRET>
                     body:   {"level": 0|1|2|3|null, "who": "<operator>"}
                     200 OK (locks voter to level, or clears lock when level is null; logged)
                     401 Unauthorized if token missing or invalid
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Optional

try:
    from . import metrics
except ModuleNotFoundError:  # pragma: no cover - fallback when prometheus_client is absent
    class _CounterChild:
        def __init__(self) -> None:
            self.value = 0

        def inc(self, amount: int = 1) -> None:
            self.value += amount

    class _FallbackCounter:
        def __init__(self) -> None:
            self._children: dict[str, _CounterChild] = {}

        def labels(self, reason: str) -> _CounterChild:
            return self._children.setdefault(reason, _CounterChild())

    class _FallbackMetrics:
        BAD_READINGS = _FallbackCounter()

    metrics = _FallbackMetrics()  # type: ignore[assignment]

from .models import Level, Reading
from .signing import verify

log = logging.getLogger("lifeboat.ingest")


class ReadingBook:
    """Latest reading per source (thread-safe)."""

    def __init__(self) -> None:
        self._d: dict[str, Reading] = {}
        self._lock = threading.Lock()

    def add(self, r: Reading) -> None:
        with self._lock:
            cur = self._d.get(r.source)
            if cur is None or r.ts >= cur.ts:
                self._d[r.source] = r

    def snapshot(self) -> list[Reading]:
        with self._lock:
            return list(self._d.values())


def handle_post(path: str, raw_body: bytes, headers: Mapping[str, str],
                secret: str, book: ReadingBook,
                on_override: Callable[[Optional[Level], str], None],
                now: Optional[float] = None) -> tuple[int, dict]:
    """Process a POST request to /readings or /override and return (status_code, response_dict)."""
    try:
        body = json.loads(raw_body.decode() if isinstance(raw_body, bytes) else raw_body)
        if not isinstance(body, dict):
            raise ValueError("expected JSON object")
    except Exception:
        metrics.BAD_READINGS.labels("malformed").inc()
        return 400, {"error": "malformed"}
    if path == "/readings":
        try:
            r = Reading.from_dict(body)
        except Exception:
            metrics.BAD_READINGS.labels("malformed").inc()
            return 400, {"error": "malformed"}
        if not verify(r, secret):
            metrics.BAD_READINGS.labels("bad_signature").inc()
            return 401, {"error": "bad_signature"}
        cur_now = now if now is not None else time.time()
        if r.expires_at <= cur_now:
            metrics.BAD_READINGS.labels("expired").inc()
            return 422, {"error": "expired"}
        book.add(r)
        return 202, {"accepted": r.source, "level": r.level}
    if path == "/override":
        if headers.get("X-Lifeboat-Token") != secret:
            return 401, {"error": "unauthorized"}
        lvl = body.get("level")
        who = str(body.get("who", "unknown"))
        target = None if lvl is None else Level(int(lvl))
        log.warning("HUMAN OVERRIDE via /override by %s -> %s", who, target)
        on_override(target, who)
        return 200, {"override": None if target is None else int(target), "who": who}
    return 404, {"error": "not_found"}


def serve(port: int, secret: str, book: ReadingBook,
          on_override: Callable[[Optional[Level], str], None]) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            self._reply(200, {"ok": True}) if self.path == "/healthz" else self._reply(404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            code, resp = handle_post(self.path, raw, self.headers, secret, book, on_override)
            self._reply(code, resp)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
