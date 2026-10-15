"""Demo web service used for C1 (emergency) and C2 (citizen portal). TIER env selects behaviour."""
import os
import time

from flask import Flask, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

TIER = os.getenv("TIER", "C1")
BURN_MS = float(os.getenv("BURN_MS", "20"))            # CPU per request
FEATURES_FILE = os.getenv("FEATURES_FILE", "/etc/lifeboat/features/degraded")

app = Flask(__name__)
REQS = Counter("http_requests_total", "requests", ["tier", "code"])
LAT = Histogram("http_request_duration_seconds", "latency", ["tier"],
                buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5))


def degraded() -> bool:
    try:
        return open(FEATURES_FILE).read().strip() == "true"   # ConfigMap volume, updated by Lifeboat at L3
    except OSError:
        return False


def burn(ms: float) -> None:
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        pass


@app.route("/")
def index():
    t0 = time.perf_counter()
    cost = BURN_MS
    if TIER == "C2" and degraded():
        cost *= 0.2                                         # extra features switched off
    burn(cost)
    LAT.labels(TIER).observe(time.perf_counter() - t0)
    REQS.labels(TIER, "200").inc()
    return {"tier": TIER, "degraded": degraded()}


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
