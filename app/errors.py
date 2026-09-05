"""Error codes and the HTTP response value object.

These live outside ``app.api`` on purpose: ``app.webhooks`` and ``app.events``
are imported *by* the API package, so they cannot import from it without a
circular import. Keeping the shared types here is what lets every layer speak
the same envelope.
"""
from __future__ import annotations

import json

ERROR_STATUS = {
    "bad_request": 400,
    "invalid_payload": 400,
    "invalid_query": 400,
    "signature_missing": 401,
    "signature_malformed": 401,
    "signature_expired": 401,
    "signature_invalid": 401,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "unprocessable": 422,
    "internal_error": 500,
}


class ApiError(Exception):
    """An error the API is willing to describe to its caller."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)

    def envelope(self, request_id: str) -> dict:
        payload = {"code": self.code, "message": self.message, "request_id": request_id}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class Response:
    """What a handler returns: a status, a body, and optional extra headers."""

    __slots__ = ("status", "payload", "body", "media_type", "headers")

    def __init__(self, status: int = 200, payload=None, body: bytes | None = None, media_type: str = "application/json; charset=utf-8", headers: dict | None = None) -> None:
        self.status = status
        self.payload = payload
        self.body = body
        self.media_type = media_type
        self.headers = headers or {}

    def encoded(self) -> bytes:
        if self.body is not None:
            return self.body
        return json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
