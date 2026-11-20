"""Webhook signatures: HMAC-SHA256 over `timestamp.body`, so a captured request cannot be replayed
after the window closes and the body cannot be edited in transit."""

from __future__ import annotations

import hashlib
import hmac
import time

MAX_SKEW_S = 300


class SignatureError(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def verify(
    secret: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    *,
    now: float | None = None,
) -> None:
    """Raises SignatureError unless the signature matches and the timestamp is fresh."""
    if not secret:
        raise SignatureError("webhook secret is not configured")
    if not timestamp_header or not signature_header:
        raise SignatureError("missing signature headers")
    try:
        ts = int(timestamp_header)
    except ValueError as e:
        raise SignatureError("bad timestamp") from e
    current = time.time() if now is None else now
    if abs(current - ts) > MAX_SKEW_S:
        raise SignatureError("timestamp outside the allowed window")
    if not hmac.compare_digest(sign(secret, body, ts), signature_header):
        raise SignatureError("signature mismatch")
