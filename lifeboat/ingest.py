"""HTTP ingest for signed readings + human override. Runs on BOTH replicas
(feeds POST to every pod via the headless Service) so both can run voter+planner."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import metrics
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
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except Exception:
                return self._reply(400)
            if self.path == "/readings":
                try:
                    r = Reading.from_dict(body)
                except Exception:
                    metrics.BAD_READINGS.labels("malformed").inc()
                    return self._reply(400)
                if not verify(r, secret):
                    metrics.BAD_READINGS.labels("bad_signature").inc()
                    return self._reply(401)
                book.add(r)
                return self._reply(202)
            if self.path == "/override":
                if self.headers.get("X-Lifeboat-Token") != secret:
                    return self._reply(401)
                lvl = body.get("level")
                on_override(None if lvl is None else Level(int(lvl)), str(body.get("who", "unknown")))
                return self._reply(200)
            self._reply(404)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
