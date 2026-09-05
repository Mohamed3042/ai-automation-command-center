"""Pagination and query helpers for the versioned API.

The error envelope and the response value object live in :mod:`app.errors` so
that modules imported by this package can raise the same errors.
"""
from __future__ import annotations

from ..errors import ERROR_STATUS, ApiError, Response  # re-exported for handlers

API_PREFIX = "/api/v1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def paginate(query: dict) -> tuple:
    limit = _int_param(query, "limit", DEFAULT_LIMIT)
    offset = _int_param(query, "offset", 0)
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError("invalid_query", "limit must be between 1 and {0}".format(MAX_LIMIT))
    if offset < 0:
        raise ApiError("invalid_query", "offset must not be negative")
    return limit, offset


def _int_param(query: dict, name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        raise ApiError("invalid_query", "{0} must be an integer".format(name)) from None


def page(rows: list, total: int, limit: int, offset: int, extra: dict | None = None) -> dict:
    payload = {
        "data": rows,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        },
    }
    if extra:
        payload.update(extra)
    return payload


def single_param(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


def require_body(body) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiError("invalid_payload", "Request body must be a JSON object")
    return body


__all__ = ["API_PREFIX", "ApiError", "Response", "ERROR_STATUS", "paginate", "page", "single_param", "require_body", "DEFAULT_LIMIT", "MAX_LIMIT"]
