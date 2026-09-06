"""Outbound signed webhooks: subscriptions, an outbox, and the delivery worker.

The alert lifecycle and workflow terminal states already produced immutable
receipts inside RelayOps. This module gives those receipts a transport: every
matching subscription gets a queued outbox row, the worker inside the scheduler
tick signs and POSTs it, each attempt is recorded, and an alert-linked delivery
writes its outcome back into that alert's timeline.

Retry policy: five attempts, exponential backoff with jitter, then `dead_letter`
(never silently dropped, and retryable by an operator).
"""
from __future__ import annotations

import fnmatch
import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import logs, metrics
from .errors import ApiError
from .signing import SIGNATURE_HEADER, signature_header

MAX_ATTEMPTS = 5
BACKOFF_CAP_SECONDS = 300
DELIVERY_TIMEOUT_SECONDS = 10
EVENT_TYPES = (
    "alert.created",
    "alert.transitioned",
    "alert.escalated",
    "workflow.run.succeeded",
    "workflow.run.failed",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def backoff_base_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("RELAYOPS_WEBHOOK_BACKOFF_SECONDS", "5")))
    except ValueError:
        return 5.0


def next_delay_seconds(attempt: int, base: float | None = None, jitter: bool = True) -> float:
    """Delay before ``attempt`` (1-based) is retried."""
    root = backoff_base_seconds() if base is None else base
    delay = min(root * (2 ** max(0, attempt - 1)), BACKOFF_CAP_SECONDS)
    if jitter and delay:
        delay += random.uniform(0, delay * 0.2)
    return delay


def event_matches(event_type: str, event_filter: str) -> bool:
    patterns = [item.strip() for item in (event_filter or "*").replace(",", " ").split() if item.strip()]
    return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in patterns) if patterns else False


def create_subscription(connection, name: str, url: str, secret: str, event_filter: str = "*", active: bool = True) -> dict:
    label = (name or "").strip()
    target = (url or "").strip()
    if not label:
        raise ApiError("invalid_payload", "Subscription name is required")
    if not target.startswith("http://") and not target.startswith("https://"):
        raise ApiError("invalid_payload", "Subscription url must be an http(s) URL")
    signing_secret = (secret or "").strip() or "whsec_" + uuid.uuid4().hex
    if connection.execute("SELECT id FROM webhook_subscriptions WHERE name=?", (label,)).fetchone():
        raise ApiError("conflict", "A subscription named {0} already exists".format(label))
    cursor = connection.execute(
        "INSERT INTO webhook_subscriptions(name,url,secret,event_filter,active,created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (label, target, signing_secret, event_filter or "*", int(bool(active)), iso(utcnow())),
    )
    connection.commit()
    row = dict(connection.execute("SELECT * FROM webhook_subscriptions WHERE id=?", (cursor.lastrowid,)).fetchone())
    row["secret"] = signing_secret
    row["active"] = bool(row["active"])
    return row


def set_subscription_active(connection, subscription_id: int, active: bool) -> dict:
    updated = connection.execute("UPDATE webhook_subscriptions SET active=? WHERE id=?", (int(bool(active)), subscription_id)).rowcount
    connection.commit()
    if not updated:
        raise ApiError("not_found", "Subscription {0} does not exist".format(subscription_id))
    return {"id": subscription_id, "active": bool(active)}


def bootstrap_from_env(connection) -> dict | None:
    """Register the Compose/CI escalation subscription without committing a secret."""
    url = os.environ.get("RELAYOPS_BOOTSTRAP_SUBSCRIPTION_URL", "").strip()
    if not url:
        return None
    name = os.environ.get("RELAYOPS_BOOTSTRAP_SUBSCRIPTION_NAME", "n8n-alert-escalation")
    existing = connection.execute("SELECT id FROM webhook_subscriptions WHERE name=?", (name,)).fetchone()
    if existing:
        connection.execute("UPDATE webhook_subscriptions SET url=?, active=1 WHERE id=?", (url, existing["id"]))
        connection.commit()
        return {"name": name, "status": "updated"}
    secret = os.environ.get("RELAYOPS_BOOTSTRAP_SUBSCRIPTION_SECRET", "").strip()
    event_filter = os.environ.get("RELAYOPS_BOOTSTRAP_SUBSCRIPTION_EVENTS", "alert.*")
    record = create_subscription(connection, name, url, secret, event_filter)
    return {"name": record["name"], "status": "created"}


def emit_event(connection, event_type: str, payload: dict, alert_id: int | None = None, event_id: str | None = None, now: datetime | None = None) -> list:
    """Queue ``event_type`` for every active subscription whose filter matches."""
    moment = now or utcnow()
    identifier = event_id or "evt_" + uuid.uuid4().hex[:16]
    body = {
        "id": identifier,
        "type": event_type,
        "created_at": iso(moment),
        "data": payload,
    }
    queued = []
    for row in connection.execute("SELECT * FROM webhook_subscriptions WHERE active=1 ORDER BY id").fetchall():
        if not event_matches(event_type, row["event_filter"]):
            continue
        cursor = connection.execute(
            "INSERT OR IGNORE INTO webhook_outbox(subscription_id,event_id,event_type,alert_id,payload_json,status,attempts,max_attempts,next_attempt_at,created_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (row["id"], identifier, event_type, alert_id, json.dumps(body, separators=(",", ":"), sort_keys=True), MAX_ATTEMPTS, iso(moment), iso(moment)),
        )
        if cursor.lastrowid and cursor.rowcount:
            queued.append({"outbox_id": cursor.lastrowid, "subscription": row["name"], "event_id": identifier, "event_type": event_type})
    if queued:
        logs.log("webhook.queued", event_type=event_type, event_id=identifier, subscriptions=len(queued))
    return queued


def _post(url: str, body: bytes, headers: dict, opener=None) -> tuple:
    request = Request(url, data=body, headers=headers, method="POST")
    sender = opener or urlopen
    started = time.perf_counter()
    try:
        with sender(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            response.read(2048)
            return int(status), "", round((time.perf_counter() - started) * 1000, 3)
    except HTTPError as exc:
        return int(exc.code), "HTTP {0}".format(exc.code), round((time.perf_counter() - started) * 1000, 3)
    except (URLError, OSError, ValueError) as exc:
        return 0, "{0}: {1}".format(type(exc).__name__, exc)[:300], round((time.perf_counter() - started) * 1000, 3)


def _record_alert_receipt(connection, alert_id: int, detail: str, moment: datetime) -> None:
    from .alerts import add_timeline

    add_timeline(connection, alert_id, "webhook_delivery", "Webhook worker", detail, moment)


def deliver_due(connection, now: datetime | None = None, opener=None, limit: int = 20) -> dict:
    """Attempt every outbox row that is due. Returns a summary for the tick."""
    moment = now or utcnow()
    rows = connection.execute(
        "SELECT o.*, s.url url, s.secret secret, s.name subscription_name FROM webhook_outbox o"
        " JOIN webhook_subscriptions s ON s.id=o.subscription_id"
        " WHERE o.status IN ('pending','retrying') AND o.next_attempt_at<=? ORDER BY o.next_attempt_at, o.id LIMIT ?",
        (iso(moment), limit),
    ).fetchall()
    delivered = 0
    retried = 0
    dead = 0
    for row in rows:
        attempt = int(row["attempts"]) + 1
        body = row["payload_json"].encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RelayOps-Webhooks/1.2",
            "X-RelayOps-Event": row["event_type"],
            "X-RelayOps-Event-Id": row["event_id"],
            "X-RelayOps-Delivery-Attempt": str(attempt),
            SIGNATURE_HEADER: signature_header(row["secret"], body, int(moment.timestamp())),
        }
        status_code, error, duration_ms = _post(row["url"], body, headers, opener)
        succeeded = 200 <= status_code < 300
        connection.execute(
            "INSERT INTO webhook_delivery_attempts(outbox_id,attempt,status,status_code,duration_ms,detail,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["id"], attempt, "delivered" if succeeded else "failed", status_code or None, duration_ms, error or "Accepted by receiver", iso(moment)),
        )
        metrics.increment("relayops_webhook_deliveries_total", {"result": "delivered" if succeeded else "failed", "event": row["event_type"]})
        if succeeded:
            connection.execute(
                "UPDATE webhook_outbox SET status='delivered',attempts=?,last_status_code=?,last_error=NULL,delivered_at=? WHERE id=?",
                (attempt, status_code, iso(moment), row["id"]),
            )
            delivered += 1
            if row["alert_id"]:
                _record_alert_receipt(connection, row["alert_id"], "Delivered {0} to {1} (HTTP {2}, attempt {3}).".format(row["event_type"], row["subscription_name"], status_code, attempt), moment)
        elif attempt >= int(row["max_attempts"]):
            connection.execute(
                "UPDATE webhook_outbox SET status='dead_letter',attempts=?,last_status_code=?,last_error=? WHERE id=?",
                (attempt, status_code or None, error or "Receiver rejected the delivery", row["id"]),
            )
            dead += 1
            if row["alert_id"]:
                _record_alert_receipt(connection, row["alert_id"], "Dead-lettered {0} to {1} after {2} attempts: {3}".format(row["event_type"], row["subscription_name"], attempt, error or status_code), moment)
        else:
            delay = next_delay_seconds(attempt)
            connection.execute(
                "UPDATE webhook_outbox SET status='retrying',attempts=?,last_status_code=?,last_error=?,next_attempt_at=? WHERE id=?",
                (attempt, status_code or None, error or "Receiver rejected the delivery", iso(moment + timedelta(seconds=delay)), row["id"]),
            )
            retried += 1
        logs.log(
            "webhook.delivery",
            level="info" if succeeded else "warning",
            event_type=row["event_type"],
            subscription=row["subscription_name"],
            attempt=attempt,
            status_code=status_code,
            duration_ms=duration_ms,
        )
    connection.commit()
    return {"attempted": len(rows), "delivered": delivered, "retrying": retried, "dead_letter": dead}


def retry_delivery(connection, outbox_id: int, now: datetime | None = None) -> dict:
    row = connection.execute("SELECT * FROM webhook_outbox WHERE id=?", (outbox_id,)).fetchone()
    if not row:
        raise ApiError("not_found", "Outbox event {0} does not exist".format(outbox_id))
    if row["status"] == "delivered":
        raise ApiError("conflict", "Outbox event {0} was already delivered".format(outbox_id))
    moment = now or utcnow()
    connection.execute(
        "UPDATE webhook_outbox SET status='pending',attempts=0,max_attempts=?,next_attempt_at=?,last_error=NULL WHERE id=?",
        (MAX_ATTEMPTS, iso(moment), outbox_id),
    )
    connection.commit()
    return {"id": outbox_id, "status": "pending", "next_attempt_at": iso(moment)}


def subscriptions_payload(connection) -> list:
    rows = connection.execute(
        "SELECT s.id,s.name,s.url,s.event_filter,s.active,s.created_at,"
        " (SELECT COUNT(*) FROM webhook_outbox o WHERE o.subscription_id=s.id) queued,"
        " (SELECT COUNT(*) FROM webhook_outbox o WHERE o.subscription_id=s.id AND o.status='delivered') delivered,"
        " (SELECT COUNT(*) FROM webhook_outbox o WHERE o.subscription_id=s.id AND o.status='dead_letter') dead_letter"
        " FROM webhook_subscriptions s ORDER BY s.id"
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["active"] = bool(item["active"])
        items.append(item)
    return items


def deliveries_payload(connection, limit: int = 50, offset: int = 0) -> tuple:
    total = connection.execute("SELECT COUNT(*) total FROM webhook_outbox").fetchone()["total"]
    rows = connection.execute(
        "SELECT o.id,o.event_id,o.event_type,o.status,o.attempts,o.max_attempts,o.next_attempt_at,o.last_status_code,o.last_error,o.created_at,o.delivered_at,o.alert_id,"
        " s.name subscription,s.url url FROM webhook_outbox o JOIN webhook_subscriptions s ON s.id=o.subscription_id"
        " ORDER BY o.created_at DESC,o.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], int(total)


def delivery_attempts(connection, outbox_id: int) -> list:
    rows = connection.execute("SELECT * FROM webhook_delivery_attempts WHERE outbox_id=? ORDER BY attempt", (outbox_id,)).fetchall()
    return [dict(row) for row in rows]
