"""OpenFaaS function entrypoint for S1 (PUB flood alerts) and S2 (NEA rainfall).

Works in three modes, all calling the same `handle()`:
  1. One-shot (`python -u -m feeds.handler --once`): what the cluster runs. The Kubernetes
     CronJobs in deploy/feeds/feeds-cronjobs.yaml (`make feeds`) start one short-lived pod per
     tick; it fetches, scores, signs and sends one reading, then exits (non-zero on failure).
  2. OpenFaaS python3-http template (`faas-cli build -f feeds/stack.yml`):
     imports `handle(event, context)` from this module.
  3. Function container (`python -u -m feeds.handler`): serves HTTP on port 8080
     (`/_/health`, `/healthz`, `POST /`, `GET /`) for faas-netes and cron-connector.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import nea, pub


def resolve_source(event=None) -> str:
    src = os.getenv("FEED_SOURCE", "S1").strip().upper()
    if isinstance(event, dict) and event.get("source"):
        src = str(event["source"]).strip().upper()
    elif hasattr(event, "path") and "nea" in str(getattr(event, "path", "")).lower():
        src = "S2"
    if src in ("S2", "NEA", "NEA-FEED"):
        return "S2"
    return "S1"


def handle(event=None, context=None) -> dict:
    src = resolve_source(event)
    if src == "S2":
        return nea.handle(event, context)
    return pub.handle(event, context)


def handle_http(method: str, path: str, raw_body: bytes = b"") -> tuple[int, dict]:
    """Process an HTTP request for the OpenFaaS watchdog server without requiring a socket."""
    if method.upper() == "GET" and path in ("/_/health", "/healthz"):
        return 200, {"ok": True, "source": resolve_source()}
    ev = None
    if raw_body:
        try:
            ev = json.loads(raw_body.decode("utf-8"))
        except Exception:
            ev = None
    try:
        return 200, handle(ev)
    except Exception as exc:
        return 502, {"error": str(exc), "source": resolve_source(ev)}


def run_once() -> int:
    """One CronJob tick. Exit 0 only if at least one Lifeboat replica accepted the reading."""
    try:
        res = handle()
    except Exception as exc:  # fetch/parse failure: no reading is sent, Lifeboat's TTL expires it
        print(json.dumps({"source": resolve_source(), "error": str(exc)}), flush=True)
        return 1
    print(json.dumps(res), flush=True)
    return 0 if any(200 <= c < 300 for c in res.get("sent", [])) else 1


def serve(port: int = 8080, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class FeedHandler(BaseHTTPRequestHandler):
        def _reply(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:  # quiet
            pass

        def do_GET(self) -> None:
            code, payload = handle_http("GET", self.path)
            self._reply(code, payload)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            code, payload = handle_http("POST", self.path, raw)
            self._reply(code, payload)

    return ThreadingHTTPServer((host, port), FeedHandler)


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--once" in sys.argv[1:]:
        sys.exit(run_once())
    port = int(os.getenv("PORT", "8080"))
    srv = serve(port=port)
    srv.serve_forever()
