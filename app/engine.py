from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from .actions import ACTION_REGISTRY, execute_action
from .alerts import create_alert
from .db import rows_as_dicts


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class InjectedStepFailure(RuntimeError):
    pass


class FailureInjector:
    """Deterministic failure plan for tests and the operator demo endpoint."""

    def __init__(self, action: str | None = None, step_id: int | None = None, fail_attempts: int = 1, message: str = "Injected adapter failure") -> None:
        if not action and step_id is None:
            raise ValueError("Failure injection requires action or step_id")
        self.action = action
        self.step_id = step_id
        self.fail_attempts = max(1, min(int(fail_attempts), 10))
        self.message = message
        self.observed_attempts = 0

    def __call__(self, step, attempt: int) -> None:
        matches = (self.action and step["action"] == self.action) or (self.step_id is not None and step["id"] == self.step_id)
        if matches:
            self.observed_attempts += 1
            if self.observed_attempts <= self.fail_attempts:
                raise InjectedStepFailure(f"{self.message} ({step['action']}, attempt {attempt})")

    @classmethod
    def from_payload(cls, payload: dict | None):
        if not payload:
            return None
        return cls(action=payload.get("action"), step_id=payload.get("step_id"), fail_attempts=payload.get("fail_attempts", 1), message=payload.get("message", "Injected adapter failure"))


def _duration_ms(start_ns: int) -> float:
    return max(0.001, round((time.perf_counter_ns() - start_ns) / 1_000_000, 3))


def _write_attempt(connection, run_id: int, step, status: str, started_at: str, duration_ms: float, output: dict, error: str | None, attempt: int, records: int) -> int:
    cursor = connection.execute(
        "INSERT INTO workflow_run_steps(run_id,step_id,status,started_at,duration_ms,output_json,error,attempt,records_processed,step_name,manual_minutes,estimate_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, step["id"], status, started_at, duration_ms, json.dumps(output, sort_keys=True), error, attempt, records, step["name"], step["manual_minutes"], step["estimate_basis"]),
    )
    return cursor.lastrowid


def _refresh_workflow_rollup(connection, workflow_id: int) -> None:
    row = connection.execute(
        "SELECT COUNT(*) runs, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) successes, AVG(CASE WHEN status!='running' THEN duration_ms END) average FROM workflow_runs WHERE workflow_id=? AND evidence_source='executed' AND started_at>=datetime('now','-30 days')",
        (workflow_id,),
    ).fetchone()
    runs = int(row["runs"] or 0)
    success_rate = round((row["successes"] or 0) / runs * 100, 2) if runs else 0
    connection.execute("UPDATE workflows SET runs_30d=?, success_rate=?, avg_duration_ms=?, updated_at=? WHERE id=?", (runs, success_rate, round(row["average"] or 0), iso(utcnow()), workflow_id))


def run_workflow(connection, workflow_id: int, trigger_type: str = "manual", failure_injector: FailureInjector | None = None) -> dict:
    workflow = connection.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not workflow:
        raise ValueError("Workflow not found")
    if not workflow["active"] and trigger_type != "manual":
        raise ValueError("Workflow is paused")
    steps = connection.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY position,id", (workflow_id,)).fetchall()
    if not steps:
        raise ValueError("Workflow has no active steps")
    started = utcnow()
    run_started_ns = time.perf_counter_ns()
    run_key = f"run-{started.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    cursor = connection.execute(
        "INSERT INTO workflow_runs(workflow_id,run_key,status,trigger_type,started_at) VALUES (?, ?, 'running', ?, ?)",
        (workflow_id, run_key, trigger_type, iso(started)),
    )
    run_id = cursor.lastrowid
    connection.commit()
    context = {"run_id": run_id, "run_key": run_key, "workflow_id": workflow_id, "trigger_type": trigger_type}
    run_steps = []
    total_records = 0
    retry_count = 0
    terminal_error = None

    for step_index, step in enumerate(steps):
        config = json.loads(step["config_json"] or "{}")
        step_succeeded = False
        for attempt in range(1, int(step["retry_limit"]) + 2):
            attempt_started = utcnow()
            attempt_started_ns = time.perf_counter_ns()
            savepoint = f"step_{step['id']}_{attempt}"
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                if failure_injector:
                    failure_injector(step, attempt)
                output = execute_action(connection, context, step, config)
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                duration = _duration_ms(attempt_started_ns)
                records = int(output.get("records", 0))
                _write_attempt(connection, run_id, step, "success", iso(attempt_started), duration, output, None, attempt, records)
                connection.commit()
                total_records += records
                run_steps.append({"step_id": step["id"], "name": step["name"], "action": step["action"], "status": "success", "duration_ms": duration, "attempt": attempt, "records_processed": records, "output": output})
                step_succeeded = True
                break
            except Exception as exc:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                duration = _duration_ms(attempt_started_ns)
                error = f"{type(exc).__name__}: {exc}"
                _write_attempt(connection, run_id, step, "failed", iso(attempt_started), duration, {}, error, attempt, 0)
                connection.commit()
                will_retry = attempt <= int(step["retry_limit"])
                run_steps.append({"step_id": step["id"], "name": step["name"], "action": step["action"], "status": "retrying" if will_retry else "failed", "duration_ms": duration, "attempt": attempt, "records_processed": 0, "error": error})
                if not will_retry:
                    terminal_error = error
                    break
                retry_count += 1
                time.sleep(max(0, int(step["retry_backoff_ms"])) / 1000)
        if not step_succeeded:
            for downstream in steps[step_index + 1:]:
                skipped_at = iso(utcnow())
                output = {"reason": "upstream step failed", "upstream_step_id": step["id"]}
                _write_attempt(connection, run_id, downstream, "skipped", skipped_at, 0.001, output, None, 0, 0)
                run_steps.append({"step_id": downstream["id"], "name": downstream["name"], "action": downstream["action"], "status": "skipped", "duration_ms": 0.001, "attempt": 0, "records_processed": 0, "output": output})
            connection.commit()
            break

    finished = utcnow()
    duration_ms = _duration_ms(run_started_ns)
    status = "failed" if terminal_error else "success"
    connection.execute(
        "UPDATE workflow_runs SET status=?,finished_at=?,duration_ms=?,records_processed=?,error=?,retries=? WHERE id=?",
        (status, iso(finished), duration_ms, total_records, terminal_error, retry_count, run_id),
    )
    if terminal_error:
        alert = create_alert(
            connection,
            f"workflow-run-{run_id}",
            f"{workflow['name']} needs attention",
            f"{terminal_error}. {retry_count} configured retry attempt(s) were executed.",
            "high",
            "Workflow engine",
            finished,
            30,
        )
    else:
        alert = None
    connection.execute(
        "INSERT INTO audit_events(event_type,title,detail,actor,created_at) VALUES ('workflow', ?, ?, ?, ?)",
        (f"{workflow['name']} {status}", f"Run {run_key} executed {len(steps)} configured actions, processed {total_records} real row result(s), and performed {retry_count} retries in {duration_ms:.3f} ms.", "Scheduler" if trigger_type == "schedule" else "Ops operator", iso(finished)),
    )
    _refresh_workflow_rollup(connection, workflow_id)
    connection.commit()
    return {"id": run_id, "run_key": run_key, "workflow": workflow["name"], "status": status, "duration_ms": duration_ms, "records_processed": total_records, "retries": retry_count, "error": terminal_error, "alert": alert, "steps": run_steps}


def hours_saved_metrics(connection) -> dict:
    rows = connection.execute(
        "SELECT wrs.records_processed,wrs.status,wrs.step_name name,wrs.manual_minutes,wrs.estimate_basis FROM workflow_run_steps wrs JOIN workflow_runs wr ON wr.id=wrs.run_id WHERE wrs.status='success' AND wr.evidence_source='executed'"
    ).fetchall()
    minutes = 0.0
    by_step: dict[str, float] = {}
    for row in rows:
        saved = float(row["manual_minutes"]) * (int(row["records_processed"]) if row["estimate_basis"] == "per_record" else 1)
        minutes += saved
        by_step[row["name"]] = by_step.get(row["name"], 0) + saved
    return {"hours": round(minutes / 60, 1), "minutes": round(minutes, 1), "formula": "Σ(step manual minutes × actual successful records; per-run steps count once)", "by_step": [{"step": key, "minutes": round(value, 1)} for key, value in sorted(by_step.items(), key=lambda item: item[1], reverse=True)]}


def workflows_payload(connection) -> dict:
    workflows = rows_as_dicts(connection.execute("SELECT * FROM workflows ORDER BY id").fetchall())
    for workflow in workflows:
        workflow["active"] = bool(workflow["active"])
        workflow["steps"] = rows_as_dicts(connection.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND active=1 ORDER BY position,id", (workflow["id"],)).fetchall())
        for step in workflow["steps"]:
            step["config"] = json.loads(step["config_json"] or "{}")
            step["active"] = bool(step["active"])
        workflow["recent_runs"] = rows_as_dicts(connection.execute("SELECT * FROM workflow_runs WHERE workflow_id=? AND evidence_source='executed' ORDER BY started_at DESC,id DESC LIMIT 8", (workflow["id"],)).fetchall())
    recent = rows_as_dicts(connection.execute("SELECT wr.*,w.name workflow_name FROM workflow_runs wr JOIN workflows w ON w.id=wr.workflow_id WHERE wr.evidence_source='executed' ORDER BY wr.started_at DESC,wr.id DESC LIMIT 20").fetchall())
    observed = connection.execute("SELECT COUNT(*) total,SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) successful FROM workflow_runs WHERE evidence_source='executed' AND started_at>=datetime('now','-30 days')").fetchone()
    total = int(observed["total"] or 0)
    successful = int(observed["successful"] or 0)
    scheduler = dict(connection.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone())
    return {
        "workflows": workflows,
        "recent_runs": recent,
        "metrics": {"active": sum(item["active"] for item in workflows), "runs_30d": sum(int(item["runs_30d"] or 0) for item in workflows), "success_rate": round(successful / total * 100, 1) if total else 0, "hours_saved": hours_saved_metrics(connection)},
        "scheduler": scheduler,
        "actions": [{"id": name, "label": name.replace(".", " · ").title()} for name in ACTION_REGISTRY],
    }


def save_workflow(connection, payload: dict, workflow_id: int | None = None) -> dict:
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    owner = str(payload.get("owner", "Operations")).strip()
    trigger_type = payload.get("trigger_type", "manual")
    schedule_cron = str(payload.get("schedule_cron", "")).strip() or None
    steps = payload.get("steps") or []
    if not name or not description:
        raise ValueError("Workflow name and description are required")
    if trigger_type not in ("manual", "event", "cron"):
        raise ValueError("Trigger type must be manual, event, or cron")
    if trigger_type == "cron" and (not schedule_cron or len(schedule_cron.split()) != 5):
        raise ValueError("Cron workflows require a five-field schedule")
    if not steps:
        raise ValueError("At least one workflow step is required")
    for index, step in enumerate(steps):
        if step.get("action") not in ACTION_REGISTRY:
            raise ValueError(f"Step {index + 1} uses an unknown action")
        if not str(step.get("name", "")).strip():
            raise ValueError(f"Step {index + 1} requires a name")
        if step.get("estimate_basis", "per_run") not in ("per_record", "per_run"):
            raise ValueError(f"Step {index + 1} has an invalid estimate basis")
        config = step.get("config", step.get("config_json", {}))
        if isinstance(config, str):
            try:
                config = json.loads(config or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Step {index + 1} configuration must be valid JSON") from exc
        if not isinstance(config, dict):
            raise ValueError(f"Step {index + 1} configuration must be a JSON object")
        step["config"] = config
    now = iso(utcnow())
    next_run_at = None
    if trigger_type == "cron":
        from .scheduler import next_cron_run
        next_run_at = iso(next_cron_run(schedule_cron, utcnow()))
    trigger_label = payload.get("trigger") or (f"Cron · {schedule_cron}" if trigger_type == "cron" else trigger_type.title())
    if workflow_id is None:
        cursor = connection.execute(
            "INSERT INTO workflows(name,description,trigger,active,runs_30d,success_rate,avg_duration_ms,owner,failure_policy,trigger_type,schedule_cron,next_run_at,created_at,updated_at) VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, trigger_label, int(bool(payload.get("active", True))), owner, "Configurable per-step retry · escalate", trigger_type, schedule_cron, next_run_at, now, now),
        )
        workflow_id = cursor.lastrowid
    else:
        if not connection.execute("SELECT id FROM workflows WHERE id=?", (workflow_id,)).fetchone():
            raise ValueError("Workflow not found")
        connection.execute("UPDATE workflows SET name=?,description=?,trigger=?,active=?,owner=?,trigger_type=?,schedule_cron=?,next_run_at=?,updated_at=? WHERE id=?", (name, description, trigger_label, int(bool(payload.get("active", True))), owner, trigger_type, schedule_cron, next_run_at, now, workflow_id))
        connection.execute("UPDATE workflow_steps SET active=0 WHERE workflow_id=?", (workflow_id,))
    for position, step in enumerate(steps, start=1):
        config = step.get("config", step.get("config_json", {}))
        if isinstance(config, str):
            config = json.loads(config or "{}")
        values = (
            position, str(step.get("name", step["action"])).strip(), step["action"], step.get("connector_slug") or None,
            json.dumps(config, sort_keys=True), max(0, min(int(step.get("retry_limit", 2)), 5)), max(0, min(int(step.get("retry_backoff_ms", 5)), 5000)),
            max(0, float(step.get("manual_minutes", 0))), step.get("estimate_basis", "per_run"), workflow_id,
        )
        step_id = step.get("id")
        belongs = step_id and connection.execute("SELECT id FROM workflow_steps WHERE id=? AND workflow_id=?", (step_id, workflow_id)).fetchone()
        if belongs:
            connection.execute("UPDATE workflow_steps SET position=?,name=?,action=?,connector_slug=?,config_json=?,retry_limit=?,retry_backoff_ms=?,manual_minutes=?,estimate_basis=?,active=1 WHERE id=?", values[:-1] + (step_id,))
        else:
            connection.execute("INSERT INTO workflow_steps(position,name,action,connector_slug,config_json,retry_limit,retry_backoff_ms,manual_minutes,estimate_basis,workflow_id,active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)", values)
    connection.execute("INSERT INTO audit_events(event_type,title,detail,actor,created_at) VALUES ('workflow_builder', ?, ?, 'Workflow builder', ?)", (f"{name} saved", f"Persisted {len(steps)} ordered executable steps.", now))
    connection.commit()
    return next(item for item in workflows_payload(connection)["workflows"] if item["id"] == workflow_id)


def toggle_workflow(connection, workflow_id: int, active: bool) -> dict:
    updated = connection.execute("UPDATE workflows SET active=?,updated_at=? WHERE id=?", (int(active), iso(utcnow()), workflow_id)).rowcount
    if not updated:
        raise ValueError("Workflow not found")
    connection.commit()
    return {"id": workflow_id, "active": bool(active)}


def evaluate_alert_rules(connection) -> dict:
    conditions = []
    for connector in connection.execute("SELECT * FROM connectors WHERE status!='healthy'").fetchall():
        conditions.append((f"connector-{connector['slug']}-rules", f"{connector['name']} health degraded", f"Latency is {connector['latency_ms']}ms with a {connector['error_rate']}% error rate.", "medium", "Connector monitor"))
    for product in connection.execute("SELECT * FROM products WHERE stock<reorder_point ORDER BY stock*1.0/reorder_point").fetchall():
        severity = "critical" if product["stock"] < product["reorder_point"] * 0.65 else "high"
        conditions.append((f"stock-{product['sku']}-rules", f"{product['name']} below reorder point", f"{product['stock']} units remain versus a reorder point of {product['reorder_point']}.", severity, "Inventory rules"))
    created = []
    deliveries = 0
    muted = 0
    moment = utcnow()
    for condition in conditions:
        result = create_alert(connection, *condition, now=moment)
        if result["created"]:
            created.append({"id": result["id"], "title": condition[1], "severity": condition[3]})
            deliveries += result["deliveries"]
            muted += int(result["muted"])
    connection.commit()
    return {"rules_evaluated": len(conditions), "alerts_created": len(created), "alerts_muted": muted, "deliveries_recorded": deliveries, "created": created}
