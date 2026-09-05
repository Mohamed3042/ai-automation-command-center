"""Slack incoming-webhook connector with a recorded fixture fallback.

With ``SLACK_WEBHOOK_URL`` set, the Block Kit message is POSTed to Slack and the
HTTP outcome is stored. Without it, the identical payload is written to
``connector_messages`` with ``mode='fixture'`` and shown in the UI labelled
"fixture" — the message is real, the transport is not, and the interface says so
rather than implying a Slack workspace exists.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import logs, metrics

CONNECTOR_SLUG = "slack"
TIMEOUT_SECONDS = 10
SEVERITY_EMOJI = {"critical": ":rotating_light:", "high": ":warning:", "medium": ":large_yellow_circle:", "low": ":white_circle:"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def public_url() -> str:
    return os.environ.get("RELAYOPS_PUBLIC_URL", "http://127.0.0.1:4173").rstrip("/")


def configured() -> bool:
    return bool(webhook_url())


def status() -> dict:
    return {
        "connector": CONNECTOR_SLUG,
        "configured": configured(),
        "mode": "live incoming webhook" if configured() else "recorded fixture adapter",
        "env_var": "SLACK_WEBHOOK_URL",
    }


def alert_blocks(alert: dict, event_type: str = "alert.escalated") -> dict:
    """Build the Block Kit body for one alert."""
    severity = str(alert.get("severity", "medium")).lower()
    title = alert.get("title") or "RelayOps alert"
    alert_id = alert.get("id") or alert.get("alert_id")
    link = "{0}/?view=alerts&alert={1}".format(public_url(), alert_id) if alert_id else public_url()
    fields = [
        {"type": "mrkdwn", "text": "*Severity*\n{0}".format(severity)},
        {"type": "mrkdwn", "text": "*State*\n{0}".format(alert.get("status", "open"))},
        {"type": "mrkdwn", "text": "*Source*\n{0}".format(alert.get("source", "RelayOps"))},
        {"type": "mrkdwn", "text": "*Escalation level*\n{0}".format(alert.get("escalation_level", 0))},
    ]
    return {
        "text": "{0} {1} — {2}".format(SEVERITY_EMOJI.get(severity, ":bell:"), severity.upper(), title),
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "{0} {1}".format(SEVERITY_EMOJI.get(severity, ":bell:"), title[:145]), "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": str(alert.get("message", ""))[:2900] or "_No detail recorded._"}},
            {"type": "section", "fields": fields},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "RelayOps · {0} · {1}".format(event_type, utcnow_iso())}]},
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open alert timeline"}, "url": link}]},
        ],
    }


def _post(url: str, body: bytes, opener=None) -> tuple:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    sender = opener or urlopen
    started = time.perf_counter()
    try:
        with sender(request, timeout=TIMEOUT_SECONDS) as response:
            code = getattr(response, "status", None) or response.getcode()
            text = response.read(512).decode("utf-8", "replace")
            return int(code), text.strip() or "ok", round((time.perf_counter() - started) * 1000, 3)
    except HTTPError as exc:
        return int(exc.code), "HTTP {0}".format(exc.code), round((time.perf_counter() - started) * 1000, 3)
    except (URLError, OSError, ValueError) as exc:
        return 0, "{0}: {1}".format(type(exc).__name__, exc)[:300], round((time.perf_counter() - started) * 1000, 3)


def notify(connection, alert: dict, event_type: str = "alert.escalated", opener=None) -> dict:
    """Send (or record) one Block Kit message and store the receipt."""
    payload = alert_blocks(alert, event_type)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    url = webhook_url()
    alert_id = alert.get("id") or alert.get("alert_id")
    if url:
        status_code, detail, duration_ms = _post(url, body, opener)
        mode = "live"
        delivered = 200 <= status_code < 300
        target = url.split("?")[0]
    else:
        status_code, detail, duration_ms = None, "SLACK_WEBHOOK_URL is not set; payload recorded as a fixture receipt.", 0.0
        mode = "fixture"
        delivered = True
        target = "fixture://slack/incoming-webhook"
    cursor = connection.execute(
        "INSERT INTO connector_messages(connector_slug,mode,target,event_type,alert_id,payload_json,status,status_code,detail,created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (CONNECTOR_SLUG, mode, target, event_type, alert_id, json.dumps(payload, sort_keys=True), "delivered" if delivered else "failed", status_code, detail, utcnow_iso()),
    )
    connection.commit()
    metrics.increment("relayops_connector_messages_total", {"connector": CONNECTOR_SLUG, "mode": mode, "result": "delivered" if delivered else "failed"})
    logs.log("connector.slack", level="info" if delivered else "warning", mode=mode, event_type=event_type, alert_id=alert_id, status_code=status_code)
    return {
        "id": cursor.lastrowid,
        "connector": CONNECTOR_SLUG,
        "mode": mode,
        "status": "delivered" if delivered else "failed",
        "status_code": status_code,
        "detail": detail,
        "duration_ms": duration_ms,
        "alert_id": alert_id,
        "event_type": event_type,
        "payload": payload,
    }


def messages_payload(connection, limit: int = 50, offset: int = 0) -> tuple:
    total = connection.execute("SELECT COUNT(*) total FROM connector_messages").fetchone()["total"]
    rows = connection.execute(
        "SELECT id,connector_slug,mode,target,event_type,alert_id,status,status_code,detail,created_at FROM connector_messages ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], int(total)
