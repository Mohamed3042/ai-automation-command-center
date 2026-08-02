from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from .alerts import enforce_escalations, iso
from .db import get_connection, rows_as_dicts
from .engine import run_workflow
from .reports import generate_report


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _field_values(field: str, minimum: int, maximum: int, normalize_weekday: bool = False) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(minimum, maximum + 1))
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ValueError("Cron step must be positive")
            values.update(range(minimum, maximum + 1, step))
            continue
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
            continue
        values.add(int(part))
    if normalize_weekday and 7 in values:
        values.remove(7)
        values.add(0)
    if not values or min(values) < minimum or max(values) > maximum:
        raise ValueError(f"Cron field {field!r} is outside {minimum}-{maximum}")
    return values


def cron_matches(moment: datetime, expression: str) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have five fields")
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7
    day_matches = moment.day in _field_values(day, 1, 31)
    weekday_matches = cron_weekday in _field_values(weekday, 0, 7, True)
    calendar_matches = (day_matches and weekday_matches) if day == "*" or weekday == "*" else (day_matches or weekday_matches)
    return (
        moment.minute in _field_values(minute, 0, 59)
        and moment.hour in _field_values(hour, 0, 23)
        and moment.month in _field_values(month, 1, 12)
        and calendar_matches
    )


def next_cron_run(expression: str, after: datetime) -> datetime:
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(527_040):
        if cron_matches(candidate, expression):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("Cron expression has no run within the next year")


class RelayScheduler:
    def __init__(self, db_path, tick_seconds: float = 1.0, now_provider=utcnow) -> None:
        self.db_path = db_path
        self.tick_seconds = max(0.1, tick_seconds)
        self.now_provider = now_provider
        self._stop_event = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        connection = get_connection(self.db_path)
        connection.execute("UPDATE scheduler_state SET status='running',last_tick_at=NULL,next_tick_at=? WHERE id=1", (iso(self.now_provider()),))
        connection.commit()
        connection.close()
        self._thread = threading.Thread(target=self._loop, name="relayops-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=max(2, self.tick_seconds * 2))
        connection = get_connection(self.db_path)
        connection.execute("UPDATE scheduler_state SET status='stopped',next_tick_at=NULL WHERE id=1")
        connection.commit()
        connection.close()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                connection = get_connection(self.db_path)
                connection.execute("UPDATE scheduler_state SET errors=errors+1,status='degraded' WHERE id=1")
                connection.execute("INSERT INTO audit_events(event_type,title,detail,actor,created_at) VALUES ('scheduler','Scheduler tick failed',?,'Background scheduler',?)", (str(exc)[:300], iso(self.now_provider())))
                connection.commit()
                connection.close()
            self._stop_event.wait(self.tick_seconds)

    def tick(self, now: datetime | None = None) -> dict:
        with self._tick_lock:
            return self._tick(now)

    def _tick(self, now: datetime | None = None) -> dict:
        moment = (now or self.now_provider()).astimezone(timezone.utc).replace(microsecond=0)
        connection = get_connection(self.db_path)
        fired = []
        errors = []
        try:
            workflows = connection.execute("SELECT * FROM workflows WHERE active=1 AND trigger_type='cron' ORDER BY id").fetchall()
            for workflow in workflows:
                if workflow["next_run_at"]:
                    next_run = datetime.fromisoformat(workflow["next_run_at"].replace("Z", "+00:00"))
                else:
                    next_run = next_cron_run(workflow["schedule_cron"], moment)
                    connection.execute("UPDATE workflows SET next_run_at=? WHERE id=?", (iso(next_run), workflow["id"]))
                if next_run <= moment:
                    scheduled_for = iso(next_run)
                    try:
                        result = run_workflow(connection, workflow["id"], "schedule")
                        status, detail = result["status"], f"{result['run_key']} · {result['records_processed']} real row result(s)"
                        if status == "failed":
                            errors.append(result["error"] or f"{workflow['name']} failed")
                    except Exception as exc:
                        status, detail = "failed", str(exc)[:300]
                        errors.append(detail)
                    connection.execute("INSERT INTO scheduler_events(job_type,job_id,scheduled_for,fired_at,status,detail) VALUES ('workflow',?,?,?,?,?)", (workflow["id"], scheduled_for, iso(moment), status, detail))
                    connection.execute("UPDATE workflows SET next_run_at=? WHERE id=?", (iso(next_cron_run(workflow["schedule_cron"], moment)), workflow["id"]))
                    fired.append({"type": "workflow", "id": workflow["id"], "name": workflow["name"], "status": status})
            schedules = connection.execute("SELECT * FROM report_schedules WHERE active=1 ORDER BY id").fetchall()
            for schedule in schedules:
                if schedule["next_run"]:
                    next_run = datetime.fromisoformat(schedule["next_run"].replace("Z", "+00:00"))
                else:
                    next_run = next_cron_run(schedule["cron_expr"], moment)
                    connection.execute("UPDATE report_schedules SET next_run=? WHERE id=?", (iso(next_run), schedule["id"]))
                if next_run <= moment:
                    scheduled_for = iso(next_run)
                    try:
                        artifacts = generate_report(connection, schedule["report_type"], generated_by="Background scheduler")
                        status, detail = "success", f"Generated {len(artifacts)} artifact(s)"
                    except Exception as exc:
                        status, detail = "failed", str(exc)[:300]
                        errors.append(detail)
                    connection.execute("INSERT INTO scheduler_events(job_type,job_id,scheduled_for,fired_at,status,detail) VALUES ('report',?,?,?,?,?)", (schedule["id"], scheduled_for, iso(moment), status, detail))
                    connection.execute("UPDATE report_schedules SET next_run=? WHERE id=?", (iso(next_cron_run(schedule["cron_expr"], moment)), schedule["id"]))
                    fired.append({"type": "report", "id": schedule["id"], "name": schedule["name"], "status": status})
            escalations = enforce_escalations(connection, moment)
            connection.execute(
                "UPDATE scheduler_state SET status=?,last_tick_at=?,next_tick_at=?,jobs_fired=jobs_fired+?,errors=errors+? WHERE id=1",
                ("running" if not errors else "degraded", iso(moment), iso(moment + timedelta(seconds=self.tick_seconds)), len(fired), len(errors)),
            )
            connection.commit()
            return {"at": iso(moment), "fired": fired, "errors": errors, "escalations": escalations}
        finally:
            connection.close()


def scheduler_payload(connection) -> dict:
    state = dict(connection.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone())
    workflows = rows_as_dicts(connection.execute("SELECT id,name,'workflow' type,next_run_at next_run,schedule_cron cron FROM workflows WHERE active=1 AND trigger_type='cron' ORDER BY next_run_at").fetchall())
    reports = rows_as_dicts(connection.execute("SELECT id,name,'report' type,next_run,cron_expr cron FROM report_schedules WHERE active=1 ORDER BY next_run").fetchall())
    events = rows_as_dicts(connection.execute("SELECT * FROM scheduler_events ORDER BY fired_at DESC,id DESC LIMIT 20").fetchall())
    jobs = sorted(workflows + reports, key=lambda item: item["next_run"] or "")
    return {"state": state, "jobs": jobs, "events": events}
