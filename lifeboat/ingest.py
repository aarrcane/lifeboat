"""HTTP ingest for signed readings + human override. Runs on BOTH replicas
(feeds POST to every pod via the headless Service) so both can run voter+planner.

Endpoints:
  GET  /healthz   -> 200 {"ok": true}
  POST /readings  -> body: {"source", "level", "ts", "expires_at", "sig"}
                     202 Accepted if valid HMAC and not expired
                     400 Bad Request if malformed JSON/fields or level not 0-3 (reason="malformed")
                     401 Unauthorized if HMAC fails (increments reason="bad_signature")
                     422 Unprocessable if already expired (reason="expired"), stamped more than
                         MAX_CLOCK_SKEW_S in the future or valid for longer than MAX_LIFETIME_S
                         (reason="future"): a future-dated reading would otherwise shadow every
                         later reading from the same source
  POST /override  -> header: X-Lifeboat-Token: <LIFEBOAT_HMAC_SECRET>
                     body:   {"level": 0|1|2|3|null, "who": "<operator>"}
                     200 OK (locks the level on EVERY replica, or clears the lock when level is
                         null; stored in the lifeboat-override ConfigMap and logged)
                     400 Bad Request if level is not null or 0-3
                     401 Unauthorized if token missing or invalid
                     503 if the override could not be stored (nothing changed: retry)
  GET  /healthz   -> 503 when the control loop has not finished a tick for a while (liveness)
"""
from __future__ import annotations

import hmac
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

MAX_CLOCK_SKEW_S = 30.0       # a reading may be stamped at most this far ahead of our clock
MAX_LIFETIME_S = 900.0        # and be valid for at most this long (feeds use 60-330 s)


class ReadingBook:
    """Latest reading per source (thread-safe)."""

    def __init__(self) -> None:
        self._d: dict[str, Reading] = {}
        self._lock = threading.Lock()

    def add(self, r: Reading, now: Optional[float] = None) -> None:
        """Keep the newest reading per source. An expired reading is always replaced, so one bad
        timestamp can never shadow the source for longer than its own lifetime."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._d.get(r.source)
            if cur is None or r.ts >= cur.ts or cur.expires_at <= now:
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
            if not r.source or not Level.L0 <= r.level <= Level.L3:
                raise ValueError("bad source or level")
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
        if r.ts > cur_now + MAX_CLOCK_SKEW_S or r.expires_at - r.ts > MAX_LIFETIME_S:
            metrics.BAD_READINGS.labels("future").inc()
            return 422, {"error": "future"}
        book.add(r, cur_now)
        return 202, {"accepted": r.source, "level": r.level}
    if path == "/override":
        token = headers.get("X-Lifeboat-Token") or ""
        if not hmac.compare_digest(token.encode(), secret.encode()):
            return 401, {"error": "unauthorized"}
        lvl = body.get("level")
        who = str(body.get("who", "unknown"))
        try:
            if isinstance(lvl, bool):
                raise ValueError("level must be a number")
            target = None if lvl is None else Level(int(lvl))
        except (TypeError, ValueError):
            return 400, {"error": "malformed", "detail": "level must be null or 0-3"}
        log.warning("HUMAN OVERRIDE via /override by %s -> %s", who, target)
        try:
            on_override(target, who)
        except Exception as e:
            log.error("override by %s could not be stored: %s", who, e)
            return 503, {"error": "override_not_stored"}
        return 200, {"override": None if target is None else int(target), "who": who}
    return 404, {"error": "not_found"}


def serve(port: int, secret: str, book: ReadingBook,
          on_override: Callable[[Optional[Level], str], None],
          healthy: Callable[[], bool] = lambda: True) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            if self.path != "/healthz":
                self._reply(404)
            elif healthy():
                self._reply(200, {"ok": True})
            else:
                self._reply(503, {"ok": False, "error": "control loop stalled"})

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                code, resp = handle_post(self.path, raw, self.headers, secret, book, on_override)
            except Exception:                              # never leave a caller without a reply
                log.exception("ingest request failed")
                code, resp = 500, {"error": "internal"}
            self._reply(code, resp)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
