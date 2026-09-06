"""Inbound signed webhooks: storefront orders and helpdesk tickets.

A request must carry a valid ``X-RelayOps-Signature`` (see :mod:`app.signing`)
within the source's tolerance window. The payload is mapped onto the rows the
v1.1 automations already consume — a staged order, or an unclassified ticket —
and the matching workflow runs immediately, so a caller's 202 receipt points at a
real run id rather than a promise.

Idempotency: the ``Idempotency-Key`` header, or the payload's own external id,
uniquely identifies a delivery. A replay returns the first receipt and creates
nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

from . import logs, metrics
from .errors import ApiError
from .engine import run_workflow
from .signing import SIGNATURE_HEADER, SignatureError, verify

SOURCES = ("orders", "tickets")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def source_row(connection, slug: str):
    row = connection.execute("SELECT * FROM webhook_sources WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise ApiError("not_found", "Unknown webhook source {0}".format(slug))
    if not row["active"]:
        raise ApiError("forbidden", "Webhook source {0} is disabled".format(slug))
    return row


def resolve_secret(connection, slug: str) -> str:
    """Environment first, seeded development secret last.

    The seeded value is published in this repository on purpose so a fresh clone
    can sign its own test request; any deployment sets the environment variable.
    """
    specific = os.environ.get("RELAYOPS_WEBHOOK_SECRET_{0}".format(slug.upper()), "").strip()
    if specific:
        return specific
    shared = os.environ.get("RELAYOPS_WEBHOOK_SECRET", "").strip()
    if shared:
        return shared
    return source_row(connection, slug)["secret"]


def _text(payload: dict, *names, **kwargs):
    for name in names:
        if "." in name:
            cursor = payload
            for part in name.split("."):
                if not isinstance(cursor, dict):
                    cursor = None
                    break
                cursor = cursor.get(part)
            value = cursor
        else:
            value = payload.get(name)
        if value not in (None, ""):
            return value
    return kwargs.get("default")


def map_order(payload: dict) -> dict:
    """Storefront order -> the canonical staged-order payload `webhook.validate` reads."""
    order_id = _text(payload, "order_id", "id", "name", "order_number")
    store = _text(payload, "store", "store_name", "location.name", "location")
    amount = _text(payload, "amount", "total_price", "total", "current_total_price")
    currency = _text(payload, "currency", "currency_code", "presentment_currency", default="USD")
    status = _text(payload, "status", "financial_status", "order_status", default="paid")
    created_at = _text(payload, "created_at", "processed_at", "createdAt")
    channel = _text(payload, "channel", "source_name", default="storefront-webhook")
    missing = [name for name, value in (("order_id", order_id), ("store", store), ("amount", amount), ("created_at", created_at)) if value in (None, "")]
    if missing:
        raise ApiError("unprocessable", "Order payload is missing required field(s)", {"missing": missing})
    try:
        numeric = float(amount)
    except (TypeError, ValueError):
        raise ApiError("unprocessable", "Order amount must be numeric", {"amount": amount}) from None
    if numeric <= 0:
        raise ApiError("unprocessable", "Order amount must be greater than zero", {"amount": numeric})
    return {
        "external_id": str(order_id),
        "channel": str(channel),
        "canonical": {
            "order_id": str(order_id),
            "store": str(store),
            "amount": numeric,
            "currency": str(currency),
            "status": str(status),
            "created_at": str(created_at),
        },
    }


def map_ticket(payload: dict) -> dict:
    """Helpdesk ticket -> an unclassified support ticket the triage workflow picks up."""
    external_id = _text(payload, "ticket_id", "id", "reference", "number")
    customer = _text(payload, "customer", "requester.name", "requester", "from")
    subject = _text(payload, "subject", "title")
    body = _text(payload, "body", "description", "text", "message")
    channel = _text(payload, "channel", "via", default="Helpdesk")
    created_at = _text(payload, "created_at", "createdAt", default=iso(utcnow()))
    missing = [name for name, value in (("subject", subject), ("body", body), ("customer", customer)) if value in (None, "")]
    if missing:
        raise ApiError("unprocessable", "Ticket payload is missing required field(s)", {"missing": missing})
    return {
        "external_id": str(external_id) if external_id else "TCK-" + uuid.uuid4().hex[:8].upper(),
        "channel": str(channel),
        "ticket": {
            "customer": str(customer),
            "subject": str(subject),
            "body": str(body),
            "channel": str(channel),
            "created_at": str(created_at),
        },
    }


MAPPERS = {"orders": map_order, "tickets": map_ticket}


def _stage_order(connection, mapped: dict, moment: datetime) -> dict:
    existing = connection.execute("SELECT id FROM staged_orders WHERE external_id=?", (mapped["external_id"],)).fetchone()
    if existing:
        raise ApiError("conflict", "Order {0} is already staged".format(mapped["external_id"]), {"staged_order_id": existing["id"]})
    cursor = connection.execute(
        "INSERT INTO staged_orders(external_id,channel,payload_json,status,staged_at) VALUES (?, ?, ?, 'pending', ?)",
        (mapped["external_id"], mapped["channel"], json.dumps(mapped["canonical"], sort_keys=True), iso(moment)),
    )
    return {"staged_order_id": cursor.lastrowid, "external_id": mapped["external_id"], "workflow_id": 1}


def _stage_ticket(connection, mapped: dict, moment: datetime) -> dict:
    ticket = mapped["ticket"]
    cursor = connection.execute(
        "INSERT INTO support_tickets(customer,subject,body,channel,category,confidence,priority,status,created_at,response_minutes,routed_queue,classified_at)"
        " VALUES (?, ?, ?, ?, 'Unclassified', 0, 'normal', 'open', ?, NULL, NULL, NULL)",
        (ticket["customer"], ticket["subject"], ticket["body"], ticket["channel"], ticket["created_at"]),
    )
    return {"ticket_id": cursor.lastrowid, "external_id": mapped["external_id"], "workflow_id": 2}


STAGERS = {"orders": _stage_order, "tickets": _stage_ticket}


def _receipt_response(row) -> dict:
    return {
        "receipt_id": row["receipt_id"],
        "source": row["source_slug"],
        "status": row["status"],
        "external_id": row["external_id"],
        "run_id": row["run_id"],
        "received_at": row["received_at"],
        "replays": row["replays"],
        "detail": row["detail"],
        "result": json.loads(row["response_json"] or "{}"),
    }


def receive(connection, slug: str, raw_body: bytes, headers, now: datetime | None = None) -> tuple:
    """Verify, map, stage and run. Returns ``(status_code, payload)``."""
    if slug not in MAPPERS:
        raise ApiError("not_found", "Unknown webhook source {0}".format(slug))
    source = source_row(connection, slug)
    moment = now or utcnow()
    signature = headers.get(SIGNATURE_HEADER) or headers.get(SIGNATURE_HEADER.lower()) or ""
    try:
        verify(resolve_secret(connection, slug), raw_body, signature, int(source["tolerance_seconds"]), now=moment.timestamp())
    except SignatureError as exc:
        metrics.increment("relayops_webhook_receipts_total", {"source": slug, "result": exc.code})
        logs.log("webhook.rejected", level="warning", source=slug, reason=exc.code)
        raise ApiError(exc.code, str(exc)) from exc
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("invalid_payload", "Webhook body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError("invalid_payload", "Webhook body must be a JSON object")

    mapped = MAPPERS[slug](payload)
    idempotency_key = (headers.get("Idempotency-Key") or headers.get("idempotency-key") or "").strip() or "{0}:{1}".format(slug, mapped["external_id"])
    existing = connection.execute("SELECT * FROM webhook_receipts WHERE source_slug=? AND idempotency_key=?", (slug, idempotency_key)).fetchone()
    if existing:
        connection.execute("UPDATE webhook_receipts SET replays=replays+1 WHERE id=?", (existing["id"],))
        connection.commit()
        metrics.increment("relayops_webhook_receipts_total", {"source": slug, "result": "duplicate"})
        logs.log("webhook.duplicate", source=slug, receipt_id=existing["receipt_id"], idempotency_key=idempotency_key)
        response = _receipt_response(connection.execute("SELECT * FROM webhook_receipts WHERE id=?", (existing["id"],)).fetchone())
        response["duplicate"] = True
        return 202, response

    receipt_id = "whr_" + uuid.uuid4().hex[:16]
    payload_sha = hashlib.sha256(raw_body).hexdigest()
    staged = STAGERS[slug](connection, mapped, moment)
    connection.commit()
    run = None
    status = "processed"
    detail = ""
    try:
        run = run_workflow(connection, staged["workflow_id"], "webhook")
        if run["status"] != "success":
            status = "workflow_failed"
            detail = run.get("error") or "Workflow run did not succeed"
    except ValueError as exc:
        status = "staged"
        detail = "Staged, but the workflow did not run: {0}".format(exc)
    result = {"staged": staged, "run": None if run is None else {"id": run["id"], "run_key": run["run_key"], "status": run["status"], "records_processed": run["records_processed"]}}
    connection.execute(
        "INSERT INTO webhook_receipts(receipt_id,source_slug,idempotency_key,external_id,status,detail,payload_sha256,received_at,run_id,response_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (receipt_id, slug, idempotency_key, mapped["external_id"], status, detail, payload_sha, iso(moment), None if run is None else run["id"], json.dumps(result, sort_keys=True)),
    )
    connection.commit()
    metrics.increment("relayops_webhook_receipts_total", {"source": slug, "result": status})
    logs.log("webhook.accepted", source=slug, receipt_id=receipt_id, external_id=mapped["external_id"], run_id=None if run is None else run["id"], status=status)
    row = connection.execute("SELECT * FROM webhook_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
    response = _receipt_response(row)
    response["duplicate"] = False
    return 202, response


def receipts_payload(connection, limit: int = 50, offset: int = 0) -> tuple:
    total = connection.execute("SELECT COUNT(*) total FROM webhook_receipts").fetchone()["total"]
    rows = connection.execute(
        "SELECT r.*, w.name workflow_name FROM webhook_receipts r"
        " LEFT JOIN workflow_runs wr ON wr.id=r.run_id LEFT JOIN workflows w ON w.id=wr.workflow_id"
        " ORDER BY r.received_at DESC,r.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("response_json", None)
        items.append(item)
    return items, int(total)


def sources_payload(connection) -> list:
    rows = connection.execute("SELECT slug,name,tolerance_seconds,workflow_id,active,created_at FROM webhook_sources ORDER BY slug").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["active"] = bool(item["active"])
        item["path"] = "/api/v1/webhooks/{0}".format(item["slug"])
        item["secret_source"] = "environment" if os.environ.get("RELAYOPS_WEBHOOK_SECRET_{0}".format(item["slug"].upper())) or os.environ.get("RELAYOPS_WEBHOOK_SECRET") else "seeded development value"
        items.append(item)
    return items


def screen_payload(connection) -> dict:
    """Everything the Webhooks screen renders in one round trip."""
    from .connectors import slack
    from .events import deliveries_payload, subscriptions_payload

    receipts, receipts_total = receipts_payload(connection, 40, 0)
    deliveries, deliveries_total = deliveries_payload(connection, 40, 0)
    messages, messages_total = slack.messages_payload(connection, 20, 0)
    subscriptions = subscriptions_payload(connection)
    delivered = sum(1 for item in deliveries if item["status"] == "delivered")
    dead = sum(1 for item in deliveries if item["status"] == "dead_letter")
    return {
        "sources": sources_payload(connection),
        "receipts": receipts,
        "subscriptions": subscriptions,
        "deliveries": deliveries,
        "connector_messages": messages,
        "slack": slack.status(),
        "metrics": {
            "receipts_total": receipts_total,
            "processed": sum(1 for item in receipts if item["status"] == "processed"),
            "replays": sum(int(item["replays"] or 0) for item in receipts),
            "deliveries_total": deliveries_total,
            "delivered": delivered,
            "dead_letter": dead,
            "active_subscriptions": sum(1 for item in subscriptions if item["active"]),
            "connector_messages_total": messages_total,
        },
    }
