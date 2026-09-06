"""Prometheus text-format metrics built from standard-library counters.

Process counters live in memory (they reset when the process restarts, which is
what a Prometheus counter means); the gauges are read from SQLite at scrape time
so they describe the store rather than this process's view of it.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

_LOCK = threading.Lock()
_COUNTERS = defaultdict(float)
_STARTED_AT = time.time()

COUNTER_HELP = {
    "relayops_http_requests_total": ("counter", "HTTP requests served, by method, route template and status."),
    "relayops_workflow_runs_total": ("counter", "Workflow runs executed by this process, by trigger and status."),
    "relayops_webhook_receipts_total": ("counter", "Inbound webhook requests, by source and outcome."),
    "relayops_webhook_deliveries_total": ("counter", "Outbound webhook delivery attempts, by outcome."),
    "relayops_connector_messages_total": ("counter", "Connector messages emitted, by connector, mode and outcome."),
}

GAUGE_HELP = {
    "relayops_workflow_runs_stored": ("gauge", "Executed workflow runs currently stored, by status."),
    "relayops_alerts": ("gauge", "Alerts currently stored, by lifecycle state."),
    "relayops_webhook_outbox": ("gauge", "Outbound webhook events currently stored, by status."),
    "relayops_webhook_receipts_stored": ("gauge", "Inbound webhook receipts currently stored, by status."),
    "relayops_webhook_subscriptions_active": ("gauge", "Active outbound webhook subscriptions."),
    "relayops_process_uptime_seconds": ("gauge", "Seconds since this RelayOps process started."),
}


def increment(name: str, labels: dict | None = None, amount: float = 1.0) -> None:
    key = (name, tuple(sorted((labels or {}).items())))
    with _LOCK:
        _COUNTERS[key] += amount


def snapshot() -> dict:
    with _LOCK:
        return dict(_COUNTERS)


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format(name: str, labels: tuple, value: float) -> str:
    if labels:
        rendered = ",".join('{0}="{1}"'.format(key, _escape(item)) for key, item in labels)
        return "{0}{{{1}}} {2:g}".format(name, rendered, value)
    return "{0} {1:g}".format(name, value)


def _gauges(connection) -> list:
    rows = []
    for row in connection.execute("SELECT status, COUNT(*) total FROM workflow_runs WHERE evidence_source='executed' GROUP BY status"):
        rows.append(("relayops_workflow_runs_stored", (("status", row["status"]),), float(row["total"])))
    for row in connection.execute("SELECT status, COUNT(*) total FROM alerts GROUP BY status"):
        rows.append(("relayops_alerts", (("state", row["status"]),), float(row["total"])))
    for row in connection.execute("SELECT status, COUNT(*) total FROM webhook_outbox GROUP BY status"):
        rows.append(("relayops_webhook_outbox", (("status", row["status"]),), float(row["total"])))
    for row in connection.execute("SELECT status, COUNT(*) total FROM webhook_receipts GROUP BY status"):
        rows.append(("relayops_webhook_receipts_stored", (("status", row["status"]),), float(row["total"])))
    subscriptions = connection.execute("SELECT COUNT(*) total FROM webhook_subscriptions WHERE active=1").fetchone()
    rows.append(("relayops_webhook_subscriptions_active", (), float(subscriptions["total"])))
    return rows


def render(connection=None) -> str:
    families = defaultdict(list)
    for key, value in snapshot().items():
        families[key[0]].append((key[1], value))
    for name in COUNTER_HELP:
        families.setdefault(name, [])
    gauge_rows = _gauges(connection) if connection is not None else []
    gauge_rows.append(("relayops_process_uptime_seconds", (), round(time.time() - _STARTED_AT, 3)))
    for name, labels, value in gauge_rows:
        families[name].append((labels, value))
    lines = []
    for name in sorted(families):
        kind, help_text = COUNTER_HELP.get(name) or GAUGE_HELP.get(name, ("untyped", "RelayOps metric."))
        lines.append("# HELP {0} {1}".format(name, help_text))
        lines.append("# TYPE {0} {1}".format(name, kind))
        for labels, value in sorted(families[name]):
            lines.append(_format(name, labels, value))
    return "\n".join(lines) + "\n"
