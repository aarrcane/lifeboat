import hmac
import hashlib

from .models import Reading


def _payload(r: Reading) -> bytes:
    return f"{r.source}|{r.level}|{r.ts:.3f}|{r.expires_at:.3f}".encode()


def sign(r: Reading, secret: str) -> Reading:
    sig = hmac.new(secret.encode(), _payload(r), hashlib.sha256).hexdigest()
    return Reading(r.source, r.level, r.ts, r.expires_at, sig)


def verify(r: Reading, secret: str) -> bool:
    if not r.sig:
        return False
    expect = hmac.new(secret.encode(), _payload(r), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, r.sig)
