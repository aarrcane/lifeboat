"""Shared by the PUB / NEA serverless functions and the replayer."""
import json
import os
from pathlib import Path
import socket
import ssl
import time
import urllib.error
import urllib.request

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - stdlib fallback when running outside venv
    requests = None

from lifeboat.models import Reading
from lifeboat.signing import sign

DEFAULT_SECRET = "dev-secret-change-me"
SECRET_PATHS = (
    Path("/var/openfaas/secrets/LIFEBOAT_HMAC_SECRET"),
    Path("/var/openfaas/secrets/lifeboat-secrets"),
)


def load_secret(secret_paths=SECRET_PATHS) -> str:
    """Read HMAC secret from env or OpenFaaS mounted secret files (/var/openfaas/secrets/)."""
    env_val = os.getenv("LIFEBOAT_HMAC_SECRET")
    if env_val:
        return env_val.strip()
    for p in secret_paths:
        try:
            if p.is_file():
                val = p.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            continue
    return DEFAULT_SECRET


SECRET = load_secret()
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
    hdrs = {"User-Agent": "lifeboat-feed/1.0", "Accept": "application/json"}
    if requests is not None:
        resp = requests.get(url, headers=hdrs, timeout=10)
        resp.raise_for_status()
        return resp.json()
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                return json.loads(resp.read().decode())
        raise
