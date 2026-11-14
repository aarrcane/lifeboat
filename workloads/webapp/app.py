"""Demo web service used for C1 (emergency) and C2 (citizen portal). TIER env selects behaviour."""
import os
import time

from flask import Flask, Response, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

TIER = os.getenv("TIER", "C1")
BURN_MS = float(os.getenv("BURN_MS", "20"))            # CPU per request
FEATURES_FILE = os.getenv("FEATURES_FILE", "/etc/lifeboat/features/degraded")

app = Flask(__name__)
REQS = Counter("http_requests_total", "requests", ["tier", "code"])
LAT = Histogram("http_request_duration_seconds", "latency", ["tier"],
                buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))


def degraded() -> bool:
    try:
        return open(FEATURES_FILE).read().strip() == "true"   # ConfigMap volume, updated by Lifeboat at L3
    except OSError:
        return False


def burn(ms: float) -> None:
    # CPU time of this thread, not wall clock: with a 250m limit a request really costs
    # `ms` of CPU, so a pod serves 250 / BURN_MS req/s (12.5 with the defaults).
    end = time.thread_time() + ms / 1000
    while time.thread_time() < end:
        pass


def observed_latency(handler_s: float, request_start: str | None, now: float) -> float:
    """Latency seen by the client: from arrival (X-Request-Start, epoch seconds set by the load
    generator) when the header is sane, so time spent queued in gunicorn counts too."""
    try:
        waited = now - float(request_start)
    except (TypeError, ValueError):
        return handler_s
    return max(handler_s, waited) if 0 <= waited <= 60 else handler_s


@app.route("/")
def index():
    t0 = time.perf_counter()
    cost = BURN_MS
    if TIER == "C2" and degraded():
        cost *= 0.2                                         # extra features switched off
    burn(cost)
    LAT.labels(TIER).observe(observed_latency(time.perf_counter() - t0,
                                              request.headers.get("X-Request-Start"), time.time()))
    REQS.labels(TIER, "200").inc()
    return {"tier": TIER, "degraded": degraded()}


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
