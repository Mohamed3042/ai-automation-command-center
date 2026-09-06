"""Handlers for the versioned RelayOps API.

Every handler receives a :class:`~app.api.RequestContext` and returns a
:class:`~app.api.core.Response`. Business logic stays in the v1.1 modules; this
layer authenticates, validates, paginates, and shapes the envelope.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import metrics
from ..alerts import alerts_payload, create_mute_rule, transition_alert
from ..connectors import slack
from ..dashboard import connectors_payload
from ..db import rows_as_dicts
from ..engine import run_workflow, toggle_workflow, workflows_payload
from ..events import create_subscription, deliveries_payload, delivery_attempts, retry_delivery, set_subscription_active, subscriptions_payload
from ..intelligence import LLMAdapter
from ..reports import REPORT_DIR, generate_report, reports_payload
from ..scheduler import scheduler_payload
from ..webhooks import receipts_payload, receive, sources_payload
from .core import ApiError, Response, page, paginate, require_body, single_param
from .keys import require_scope


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- system


def health(ctx) -> Response:
    from ..db import get_connection

    try:
        probe = get_connection(ctx.db_path)
        probe.execute("SELECT 1").fetchone()
        probe.close()
        database = "connected"
    except Exception as exc:  # pragma: no cover - only on a broken volume
        return Response(503, {"status": "degraded", "service": "relayops", "version": ctx.version, "database": "unavailable", "error": str(exc)[:160]})
    return Response(200, {
        "status": "ok",
        "service": "relayops",
        "version": ctx.version,
        "database": database,
        "scheduler": "running" if ctx.scheduler_running else "stopped",
        "llm": LLMAdapter().status(),
        "slack": slack.status(),
    })


def ready(ctx) -> Response:
    """Readiness: the store answers and the scheduler thread is alive."""
    scheduler_ok = ctx.scheduler_running
    try:
        ctx.connection.execute("SELECT COUNT(*) FROM workflows").fetchone()
        database_ok = True
    except Exception:  # pragma: no cover - only on a broken volume
        database_ok = False
    checks = {"database": database_ok, "scheduler": scheduler_ok}
    ready_now = all(checks.values())
    return Response(200 if ready_now else 503, {"ready": ready_now, "checks": checks, "checked_at": _now_iso()})


def prometheus_metrics(ctx) -> Response:
    return Response(200, body=metrics.render(ctx.connection).encode("utf-8"), media_type="text/plain; version=0.0.4; charset=utf-8")


# --------------------------------------------------------------------------- workflows


def list_workflows(ctx) -> Response:
    require_scope(ctx.principal, "workflows:read")
    limit, offset = paginate(ctx.query)
    payload = workflows_payload(ctx.connection)
    items = payload["workflows"]
    active_only = single_param(ctx.query, "active")
    if active_only is not None:
        wanted = active_only.lower() in ("1", "true", "yes")
        items = [item for item in items if item["active"] is wanted]
    total = len(items)
    return Response(200, page(items[offset:offset + limit], total, limit, offset, {"metrics": payload["metrics"], "actions": payload["actions"]}))


def get_workflow(ctx) -> Response:
    require_scope(ctx.principal, "workflows:read")
    workflow_id = int(ctx.params["workflow_id"])
    for item in workflows_payload(ctx.connection)["workflows"]:
        if item["id"] == workflow_id:
            return Response(200, item)
    raise ApiError("not_found", "Workflow {0} does not exist".format(workflow_id))


def run_workflow_route(ctx) -> Response:
    require_scope(ctx.principal, "workflows:run")
    body = require_body(ctx.body)
    workflow_id = int(ctx.params["workflow_id"])
    try:
        result = run_workflow(ctx.connection, workflow_id, str(body.get("trigger_type", "api")))
    except ValueError as exc:
        raise ApiError("unprocessable", str(exc)) from exc
    return Response(201, result)


def toggle_workflow_route(ctx) -> Response:
    require_scope(ctx.principal, "workflows:write")
    body = require_body(ctx.body)
    if "active" not in body:
        raise ApiError("invalid_payload", "active is required")
    try:
        return Response(200, toggle_workflow(ctx.connection, int(ctx.params["workflow_id"]), bool(body["active"])))
    except ValueError as exc:
        raise ApiError("not_found", str(exc)) from exc


# --------------------------------------------------------------------------- runs


def list_runs(ctx) -> Response:
    require_scope(ctx.principal, "runs:read")
    limit, offset = paginate(ctx.query)
    filters = []
    args = []
    status = single_param(ctx.query, "status")
    if status:
        filters.append("wr.status=?")
        args.append(status)
    workflow_id = single_param(ctx.query, "workflow_id")
    if workflow_id:
        filters.append("wr.workflow_id=?")
        args.append(int(workflow_id))
    where = " AND ".join(["wr.evidence_source='executed'"] + filters)
    total = ctx.connection.execute("SELECT COUNT(*) total FROM workflow_runs wr WHERE " + where, args).fetchone()["total"]
    rows = ctx.connection.execute(
        "SELECT wr.*, w.name workflow_name FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id WHERE " + where + " ORDER BY wr.started_at DESC,wr.id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return Response(200, page(rows_as_dicts(rows), int(total), limit, offset))


def get_run(ctx) -> Response:
    require_scope(ctx.principal, "runs:read")
    run_id = int(ctx.params["run_id"])
    row = ctx.connection.execute("SELECT wr.*, w.name workflow_name FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id WHERE wr.id=?", (run_id,)).fetchone()
    if not row:
        raise ApiError("not_found", "Run {0} does not exist".format(run_id))
    payload = dict(row)
    payload["attempts"] = rows_as_dicts(ctx.connection.execute("SELECT * FROM workflow_run_steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall())
    return Response(200, payload)


def list_run_attempts(ctx) -> Response:
    require_scope(ctx.principal, "runs:read")
    limit, offset = paginate(ctx.query)
    run_id = int(ctx.params["run_id"])
    if not ctx.connection.execute("SELECT id FROM workflow_runs WHERE id=?", (run_id,)).fetchone():
        raise ApiError("not_found", "Run {0} does not exist".format(run_id))
    total = ctx.connection.execute("SELECT COUNT(*) total FROM workflow_run_steps WHERE run_id=?", (run_id,)).fetchone()["total"]
    rows = ctx.connection.execute("SELECT * FROM workflow_run_steps WHERE run_id=? ORDER BY id LIMIT ? OFFSET ?", (run_id, limit, offset)).fetchall()
    return Response(200, page(rows_as_dicts(rows), int(total), limit, offset))


# --------------------------------------------------------------------------- alerts


def list_alerts(ctx) -> Response:
    require_scope(ctx.principal, "alerts:read")
    limit, offset = paginate(ctx.query)
    payload = alerts_payload(ctx.connection)
    items = payload["alerts"]
    status = single_param(ctx.query, "status")
    if status:
        items = [item for item in items if item["status"] == status]
    severity = single_param(ctx.query, "severity")
    if severity:
        items = [item for item in items if item["severity"] == severity]
    return Response(200, page(items[offset:offset + limit], len(items), limit, offset, {"counts": payload["counts"], "lifecycle": payload["lifecycle"]}))


def get_alert(ctx) -> Response:
    require_scope(ctx.principal, "alerts:read")
    alert_id = int(ctx.params["alert_id"])
    for item in alerts_payload(ctx.connection)["alerts"]:
        if item["id"] == alert_id:
            return Response(200, item)
    raise ApiError("not_found", "Alert {0} does not exist".format(alert_id))


def transition_alert_route(ctx) -> Response:
    require_scope(ctx.principal, "alerts:write")
    body = require_body(ctx.body)
    try:
        return Response(200, transition_alert(ctx.connection, int(ctx.params["alert_id"]), str(body.get("status", "")), actor=str(body.get("actor", "API client")), note=str(body.get("note", ""))))
    except ValueError as exc:
        raise ApiError("unprocessable", str(exc)) from exc


def create_mute_route(ctx) -> Response:
    require_scope(ctx.principal, "alerts:write")
    body = require_body(ctx.body)
    try:
        return Response(201, create_mute_rule(ctx.connection, str(body.get("source_pattern", "*")), str(body.get("severity", "*")), int(body.get("duration_minutes", 60)), str(body.get("reason", ""))))
    except ValueError as exc:
        raise ApiError("invalid_payload", str(exc)) from exc


# --------------------------------------------------------------------------- reports


def list_reports(ctx) -> Response:
    require_scope(ctx.principal, "reports:read")
    limit, offset = paginate(ctx.query)
    payload = reports_payload(ctx.connection)
    items = payload["reports"]
    return Response(200, page(items[offset:offset + limit], len(items), limit, offset, {"schedules": payload["schedules"], "metrics": payload["metrics"]}))


def generate_report_route(ctx) -> Response:
    require_scope(ctx.principal, "reports:write")
    body = require_body(ctx.body)
    try:
        artifacts = generate_report(ctx.connection, str(body.get("report_type", "daily")), generated_by="API client {0}".format(ctx.principal.get("name", "unknown")))
    except ValueError as exc:
        raise ApiError("invalid_payload", str(exc)) from exc
    return Response(201, {"artifacts": artifacts})


def download_report_route(ctx) -> Response:
    require_scope(ctx.principal, "reports:read")
    report_id = int(ctx.params["report_id"])
    row = ctx.connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        raise ApiError("not_found", "Report {0} does not exist".format(report_id))
    target = (REPORT_DIR / row["path"]).resolve()
    root = REPORT_DIR.resolve()
    if root not in target.parents or not target.exists():
        raise ApiError("not_found", "Report artifact {0} is no longer on disk".format(row["path"]))
    media = "text/html; charset=utf-8" if row["format"] == "HTML" else "text/csv; charset=utf-8"
    return Response(200, body=target.read_bytes(), media_type=media, headers={"Content-Disposition": 'attachment; filename="{0}"'.format(target.name)})


# --------------------------------------------------------------------------- scheduler / connectors


def get_scheduler(ctx) -> Response:
    require_scope(ctx.principal, "scheduler:read")
    return Response(200, scheduler_payload(ctx.connection))


def list_scheduler_jobs(ctx) -> Response:
    require_scope(ctx.principal, "scheduler:read")
    limit, offset = paginate(ctx.query)
    jobs = scheduler_payload(ctx.connection)["jobs"]
    return Response(200, page(jobs[offset:offset + limit], len(jobs), limit, offset))


def list_connectors(ctx) -> Response:
    require_scope(ctx.principal, "connectors:read")
    payload = connectors_payload(ctx.connection)
    payload["slack"] = slack.status()
    return Response(200, payload)


def slack_notify_route(ctx) -> Response:
    """Post one alert to Slack (or record the fixture receipt)."""
    require_scope(ctx.principal, "connectors:write")
    body = require_body(ctx.body)
    alert = body.get("alert")
    if not isinstance(alert, dict):
        alert_id = body.get("alert_id")
        if alert_id is None:
            raise ApiError("invalid_payload", "Provide alert_id, or an alert object")
        row = ctx.connection.execute("SELECT * FROM alerts WHERE id=?", (int(alert_id),)).fetchone()
        if not row:
            raise ApiError("not_found", "Alert {0} does not exist".format(alert_id))
        alert = dict(row)
    result = slack.notify(ctx.connection, alert, str(body.get("event_type", "alert.escalated")))
    return Response(201, result)


def list_connector_messages(ctx) -> Response:
    require_scope(ctx.principal, "connectors:read")
    limit, offset = paginate(ctx.query)
    rows, total = slack.messages_payload(ctx.connection, limit, offset)
    return Response(200, page(rows, total, limit, offset))


# --------------------------------------------------------------------------- webhooks


def _webhook_route(slug: str):
    def handler(ctx) -> Response:
        status, payload = receive(ctx.connection, slug, ctx.raw_body, ctx.headers)
        return Response(status, payload)

    handler.__name__ = "receive_{0}_webhook".format(slug)
    return handler


def list_webhook_sources(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:read")
    return Response(200, {"data": sources_payload(ctx.connection)})


def list_webhook_receipts(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:read")
    limit, offset = paginate(ctx.query)
    rows, total = receipts_payload(ctx.connection, limit, offset)
    return Response(200, page(rows, total, limit, offset))


def list_subscriptions(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:read")
    return Response(200, {"data": subscriptions_payload(ctx.connection)})


def create_subscription_route(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:write")
    body = require_body(ctx.body)
    record = create_subscription(ctx.connection, str(body.get("name", "")), str(body.get("url", "")), str(body.get("secret", "")), str(body.get("event_filter", "*")), bool(body.get("active", True)))
    return Response(201, record)


def toggle_subscription_route(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:write")
    body = require_body(ctx.body)
    return Response(200, set_subscription_active(ctx.connection, int(ctx.params["subscription_id"]), bool(body.get("active", True))))


def list_deliveries(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:read")
    limit, offset = paginate(ctx.query)
    rows, total = deliveries_payload(ctx.connection, limit, offset)
    return Response(200, page(rows, total, limit, offset))


def get_delivery(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:read")
    delivery_id = int(ctx.params["delivery_id"])
    rows, _ = deliveries_payload(ctx.connection, 1000, 0)
    for row in rows:
        if row["id"] == delivery_id:
            row["attempts_log"] = delivery_attempts(ctx.connection, delivery_id)
            row["payload"] = json.loads(ctx.connection.execute("SELECT payload_json FROM webhook_outbox WHERE id=?", (delivery_id,)).fetchone()["payload_json"])
            return Response(200, row)
    raise ApiError("not_found", "Delivery {0} does not exist".format(delivery_id))


def retry_delivery_route(ctx) -> Response:
    require_scope(ctx.principal, "webhooks:write")
    return Response(200, retry_delivery(ctx.connection, int(ctx.params["delivery_id"])))
