from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .db import rows_as_dicts


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def run_workflow(connection, workflow_id: int, trigger_type: str = "manual", force_error: bool = False) -> dict:
    workflow = connection.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not workflow:
        raise ValueError("Workflow not found")
    steps = connection.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY position", (workflow_id,)).fetchall()
    started = _utcnow()
    run_key = f"run-{started.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    cursor = connection.execute(
        "INSERT INTO workflow_runs(workflow_id, run_key, status, trigger_type, started_at) VALUES (?, ?, 'running', ?, ?)",
        (workflow_id, run_key, trigger_type, started.isoformat().replace("+00:00", "Z")),
    )
    run_id = cursor.lastrowid
    total_duration = 0
    records = 0
    failed = False
    error = None
    run_steps = []
    for index, step in enumerate(steps):
        step_started = started + timedelta(milliseconds=total_duration)
        should_fail = force_error and index == min(2, len(steps) - 1)
        duration = 185 + step["position"] * 117 + workflow_id * 29
        total_duration += duration
        if should_fail:
            failed = True
            error = f"Simulated adapter timeout in {step['name']} after 3 retry attempts"
            connection.execute(
                "INSERT INTO workflow_run_steps(run_id, step_id, status, started_at, duration_ms, output_json, error, attempt) VALUES (?, ?, 'failed', ?, ?, '{}', ?, 3)",
                (run_id, step["id"], step_started.isoformat().replace("+00:00", "Z"), duration * 3, error),
            )
            run_steps.append({"name": step["name"], "action": step["action"], "status": "failed", "duration_ms": duration * 3, "attempt": 3, "error": error})
            total_duration += duration * 2
            break
        step_records = 86 + workflow_id * 31 + index * 17
        records = max(records, step_records)
        output = {"records": step_records, "adapter": step["connector_slug"] or "internal", "validated": True}
        connection.execute(
            "INSERT INTO workflow_run_steps(run_id, step_id, status, started_at, duration_ms, output_json, attempt) VALUES (?, ?, 'success', ?, ?, ?, 1)",
            (run_id, step["id"], step_started.isoformat().replace("+00:00", "Z"), duration, json.dumps(output)),
        )
        run_steps.append({"name": step["name"], "action": step["action"], "status": "success", "duration_ms": duration, "attempt": 1, "output": output})
    if failed:
        completed_step_ids = {item["name"] for item in run_steps}
        for step in steps:
            if step["name"] not in completed_step_ids:
                connection.execute(
                    "INSERT INTO workflow_run_steps(run_id, step_id, status, started_at, duration_ms, output_json, attempt) VALUES (?, ?, 'skipped', ?, 0, '{}', 0)",
                    (run_id, step["id"], (started + timedelta(milliseconds=total_duration)).isoformat().replace("+00:00", "Z")),
                )
                run_steps.append({"name": step["name"], "action": step["action"], "status": "skipped", "duration_ms": 0, "attempt": 0})
    finished = started + timedelta(milliseconds=total_duration)
    status = "failed" if failed else "success"
    connection.execute(
        "UPDATE workflow_runs SET status=?, finished_at=?, duration_ms=?, records_processed=?, error=?, retries=? WHERE id=?",
        (status, finished.isoformat().replace("+00:00", "Z"), total_duration, records, error, 3 if failed else 0, run_id),
    )
    if failed:
        alert_cursor = connection.execute(
            "INSERT INTO alerts(dedupe_key, title, message, severity, source, status, created_at, escalates_at) VALUES (?, ?, ?, 'high', 'Workflow engine', 'open', ?, ?)",
            (f"workflow-run-{run_id}", f"{workflow['name']} needs attention", error, started.isoformat().replace("+00:00", "Z"), (started + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")),
        )
        alert_id = alert_cursor.lastrowid
        for channel, recipient in (("In-app", "Ops Command Center"), ("WhatsApp", "Duty manager group")):
            connection.execute(
                "INSERT INTO alert_deliveries(alert_id, channel, recipient, status, attempt, sent_at, detail) VALUES (?, ?, ?, 'delivered', 1, ?, ?)",
                (alert_id, channel, recipient, started.isoformat().replace("+00:00", "Z"), "Failure policy escalation delivered by demo adapter"),
            )
    connection.execute(
        "INSERT INTO audit_events(event_type, title, detail, actor, created_at) VALUES ('workflow', ?, ?, 'Manual operator', ?)",
        (f"{workflow['name']} {status}", f"Run {run_key} processed {records} records with {3 if failed else 0} retries.", started.isoformat().replace("+00:00", "Z")),
    )
    connection.commit()
    return {"id": run_id, "run_key": run_key, "workflow": workflow["name"], "status": status, "duration_ms": total_duration, "records_processed": records, "retries": 3 if failed else 0, "error": error, "steps": run_steps}


def workflows_payload(connection) -> dict:
    workflows = rows_as_dicts(connection.execute("SELECT * FROM workflows ORDER BY id").fetchall())
    for workflow in workflows:
        workflow["active"] = bool(workflow["active"])
        workflow["steps"] = rows_as_dicts(connection.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY position", (workflow["id"],)).fetchall())
        workflow["recent_runs"] = rows_as_dicts(connection.execute("SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY started_at DESC LIMIT 5", (workflow["id"],)).fetchall())
    recent = rows_as_dicts(connection.execute(
        "SELECT wr.*, w.name workflow_name FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id ORDER BY wr.started_at DESC LIMIT 16"
    ).fetchall())
    runs_30d = sum(item["runs_30d"] for item in workflows)
    weighted_success = sum(item["runs_30d"] * item["success_rate"] for item in workflows) / runs_30d if runs_30d else 0
    return {"workflows": workflows, "recent_runs": recent, "metrics": {"active": sum(item["active"] for item in workflows), "runs_30d": runs_30d, "success_rate": round(weighted_success, 1), "hours_saved": 286}}


def evaluate_alert_rules(connection) -> dict:
    now = _utcnow()
    conditions = []
    for connector in connection.execute("SELECT * FROM connectors WHERE status != 'healthy'").fetchall():
        conditions.append((f"connector-{connector['slug']}-rules", f"{connector['name']} health degraded", f"Latency is {connector['latency_ms']}ms with a {connector['error_rate']}% error rate.", "medium", "Connector monitor"))
    for product in connection.execute("SELECT * FROM products WHERE stock < reorder_point ORDER BY stock * 1.0 / reorder_point").fetchall():
        severity = "critical" if product["stock"] < product["reorder_point"] * 0.65 else "high"
        conditions.append((f"stock-{product['sku']}-rules", f"{product['name']} below reorder point", f"{product['stock']} units remain versus a reorder point of {product['reorder_point']}.", severity, "Inventory rules"))
    created = []
    deliveries = 0
    for dedupe_key, title, message, severity, source in conditions:
        exists = connection.execute("SELECT id FROM alerts WHERE dedupe_key=? AND status IN ('open','investigating')", (dedupe_key,)).fetchone()
        if exists:
            continue
        cursor = connection.execute(
            "INSERT INTO alerts(dedupe_key, title, message, severity, source, status, created_at, escalates_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (dedupe_key, title, message, severity, source, now.isoformat().replace("+00:00", "Z"), (now + timedelta(minutes=15 if severity == "critical" else 30)).isoformat().replace("+00:00", "Z")),
        )
        alert_id = cursor.lastrowid
        channels = [("In-app", "Ops Command Center")]
        if severity in ("critical", "high"):
            channels.append(("WhatsApp", "Duty manager group"))
        for channel, recipient in channels:
            connection.execute(
                "INSERT INTO alert_deliveries(alert_id, channel, recipient, status, attempt, sent_at, detail) VALUES (?, ?, ?, 'delivered', 1, ?, 'Threshold rule matched; demo receipt recorded')",
                (alert_id, channel, recipient, now.isoformat().replace("+00:00", "Z")),
            )
            deliveries += 1
        created.append({"id": alert_id, "title": title, "severity": severity})
    connection.commit()
    return {"rules_evaluated": len(conditions), "alerts_created": len(created), "deliveries_recorded": deliveries, "created": created}
