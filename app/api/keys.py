"""Scoped API keys, hashed at rest.

A key is minted once and shown once::

    relay_<key_id>_<secret>

Only ``key_id`` is stored in clear; the secret is stored as a PBKDF2-HMAC-SHA256
hash with a per-key salt, so a copy of ``command_center.db`` does not hand anyone
a working credential.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

from .core import ApiError

TOKEN_PREFIX = "relay"
PBKDF2_ITERATIONS = 200_000
ALL_SCOPES = (
    "workflows:read",
    "workflows:write",
    "workflows:run",
    "runs:read",
    "alerts:read",
    "alerts:write",
    "reports:read",
    "reports:write",
    "scheduler:read",
    "connectors:read",
    "connectors:write",
    "webhooks:read",
    "webhooks:write",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash(secret: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt.encode("utf-8"), iterations).hex()


def normalize_scopes(scopes) -> str:
    if isinstance(scopes, str):
        requested = [item.strip() for item in scopes.replace(",", " ").split() if item.strip()]
    else:
        requested = [str(item).strip() for item in (scopes or []) if str(item).strip()]
    if not requested or requested == ["*"]:
        return "*"
    unknown = sorted(set(requested) - set(ALL_SCOPES))
    if unknown:
        raise ApiError("invalid_payload", "Unknown scope(s): {0}".format(", ".join(unknown)), {"allowed": list(ALL_SCOPES)})
    return " ".join(sorted(set(requested)))


def parse_token(token: str) -> tuple:
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise ApiError("unauthorized", "API token must look like relay_<id>_<secret>")
    return parts[1], parts[2]


def create_key(connection, name: str, scopes="*", token: str | None = None) -> dict:
    """Mint a key. Returns the row plus the one-time ``token``."""
    label = (name or "").strip()
    if not label:
        raise ApiError("invalid_payload", "API key name is required")
    scope_text = normalize_scopes(scopes)
    if token:
        key_id, secret = parse_token(token)
    else:
        key_id, secret = secrets.token_hex(6), secrets.token_hex(24)
    salt = secrets.token_hex(16)
    created = _now()
    connection.execute(
        "INSERT INTO api_keys(key_id,name,scopes,salt,secret_hash,iterations,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key_id, label, scope_text, salt, _hash(secret, salt), PBKDF2_ITERATIONS, created),
    )
    connection.commit()
    return {
        "key_id": key_id,
        "name": label,
        "scopes": scope_text,
        "created_at": created,
        "token": "{0}_{1}_{2}".format(TOKEN_PREFIX, key_id, secret),
    }


def bootstrap_from_env(connection) -> dict | None:
    """Install ``RELAYOPS_BOOTSTRAP_API_KEY`` so a container starts usable.

    Intended for Compose and CI, where the same token is handed to RelayOps and
    to the caller. The token itself never enters the repository.
    """
    token = os.environ.get("RELAYOPS_BOOTSTRAP_API_KEY", "").strip()
    if not token:
        return None
    key_id, _ = parse_token(token)
    existing = connection.execute("SELECT key_id FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    if existing:
        return {"key_id": key_id, "status": "existing"}
    name = os.environ.get("RELAYOPS_BOOTSTRAP_API_KEY_NAME", "bootstrap")
    scopes = os.environ.get("RELAYOPS_BOOTSTRAP_API_KEY_SCOPES", "*")
    record = create_key(connection, name, scopes, token=token)
    return {"key_id": record["key_id"], "status": "created"}


def revoke_key(connection, key_id: str) -> dict:
    updated = connection.execute("UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL", (_now(), key_id)).rowcount
    connection.commit()
    if not updated:
        raise ApiError("not_found", "No active API key with id {0}".format(key_id))
    return {"key_id": key_id, "revoked": True}


def list_keys(connection) -> list:
    rows = connection.execute("SELECT key_id,name,scopes,created_at,last_used_at,revoked_at FROM api_keys ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def authenticate(connection, authorization: str) -> dict:
    """Resolve an ``Authorization: Bearer relay_...`` header to a key row."""
    header = (authorization or "").strip()
    if not header:
        raise ApiError("unauthorized", "Authorization header is required")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError("unauthorized", "Authorization header must use the Bearer scheme")
    key_id, secret = parse_token(token.strip())
    row = connection.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    if not row:
        raise ApiError("unauthorized", "API key is not recognised")
    if row["revoked_at"]:
        raise ApiError("unauthorized", "API key was revoked at {0}".format(row["revoked_at"]))
    candidate = _hash(secret, row["salt"], int(row["iterations"]))
    if not hmac.compare_digest(candidate, row["secret_hash"]):
        raise ApiError("unauthorized", "API key is not recognised")
    connection.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (_now(), key_id))
    connection.commit()
    return {"key_id": key_id, "name": row["name"], "scopes": row["scopes"]}


def require_scope(principal: dict, scope: str) -> None:
    granted = principal.get("scopes", "")
    if granted == "*" or scope in granted.split():
        return
    raise ApiError("forbidden", "This API key is missing the {0} scope".format(scope), {"granted": granted})
