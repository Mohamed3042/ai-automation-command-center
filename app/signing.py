"""HMAC-SHA256 request signing shared by inbound and outbound webhooks.

Header format (identical in spirit to the ATMPL repository so the two systems
interoperate)::

    X-RelayOps-Signature: t=1754130271,v1=6f1c...

``v1`` is ``HMAC_SHA256(secret, f"{t}.{raw_body}")`` in lowercase hex, where
``raw_body`` is the exact bytes on the wire. Verification is constant-time and
rejects a timestamp outside the tolerance window, which is what makes a captured
request unusable later.
"""
from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-RelayOps-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(ValueError):
    """Raised when a signature is missing, malformed, stale, or wrong."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.code = code


def signed_payload(timestamp: int, body: bytes) -> bytes:
    return "{0}.".format(int(timestamp)).encode("utf-8") + body


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), signed_payload(timestamp, body), hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    moment = int(time.time()) if timestamp is None else int(timestamp)
    return "t={0},v1={1}".format(moment, compute_signature(secret, moment, body))


def parse_signature_header(header: str) -> tuple:
    if not header:
        raise SignatureError("Signature header is missing", "signature_missing")
    timestamp = None
    candidates = []
    for part in header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("Signature timestamp is not an integer", "signature_malformed") from exc
        elif key == "v1":
            candidates.append(value.strip())
    if timestamp is None or not candidates:
        raise SignatureError("Signature header must contain t= and v1=", "signature_malformed")
    return timestamp, candidates


def verify(secret: str, body: bytes, header: str, tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS, now: float | None = None) -> int:
    """Return the signed timestamp, or raise :class:`SignatureError`."""
    timestamp, candidates = parse_signature_header(header)
    moment = time.time() if now is None else now
    if tolerance_seconds >= 0 and abs(moment - timestamp) > tolerance_seconds:
        raise SignatureError("Signature timestamp is outside the {0}s tolerance window".format(tolerance_seconds), "signature_expired")
    expected = compute_signature(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SignatureError("Signature does not match the request body", "signature_invalid")
    return timestamp
