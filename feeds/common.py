"""Shared by the PUB / NEA serverless functions and the replayer."""
import json
import os
import socket
import time
import urllib.error
import urllib.request

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - stdlib fallback when running outside venv
    requests = None

from lifeboat.models import Reading
from lifeboat.signing import sign

SECRET = os.getenv("LIFEBOAT_HMAC_SECRET", "dev-secret-change-me")
INGEST_HOST = os.getenv("LIFEBOAT_INGEST_HOST", "lifeboat-ingest.lifeboat.svc.cluster.local")
INGEST_PORT = int(os.getenv("LIFEBOAT_INGEST_PORT", "8081"))


def make_reading(source: str, level: int, ttl_s: float) -> Reading:
    now = time.time()
    return sign(Reading(source, level, now, now + ttl_s), SECRET)


def targets() -> list[str]:
    """Headless Service => one IP per Lifeboat pod. Send to ALL so both replicas see every reading."""
    try:
        infos = socket.getaddrinfo(INGEST_HOST, INGEST_PORT, type=socket.SOCK_STREAM)
        return sorted({f"http://{i[4][0]}:{INGEST_PORT}" for i in infos})
    except socket.gaierror:
        return [f"http://{INGEST_HOST}:{INGEST_PORT}"]


def _post_json(url: str, payload: dict, timeout: float = 3.0) -> int:
    if requests is not None:
        try:
            return requests.post(url, json=payload, timeout=timeout).status_code
        except requests.RequestException:
            return 0
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def send(r: Reading) -> list[int]:
    return [_post_json(f"{url}/readings", r.to_dict(), timeout=3) for url in targets()]


def get_json(url: str):
    if requests is not None:
        return requests.get(url, timeout=10).json()
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())
