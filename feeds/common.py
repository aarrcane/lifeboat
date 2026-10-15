"""Shared by the PUB / NEA serverless functions and the replayer."""
import json
import os
import socket
import time

import requests

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


def send(r: Reading) -> list[int]:
    codes = []
    for url in targets():
        try:
            codes.append(requests.post(f"{url}/readings", json=r.to_dict(), timeout=3).status_code)
        except requests.RequestException:
            codes.append(0)
    return codes


def get_json(url: str):
    return requests.get(url, timeout=10).json()
