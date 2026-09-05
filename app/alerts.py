from __future__ import annotations

import fnmatch
from datetime import datetime, timedelta, timezone

from .db import rows_as_dicts
from .events import emit_event


VALID_TRANSITIONS = {
    "open": "acknowledged",
    "acknowledged": "investigating",
    "investigating": "resolved",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def alert_event_payload(connection, alert_id: int) -> dict:
    """The alert as an outbound event body: the row plus its full timeline."""
    row = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if not row:
        return {"alert": {"id": alert_id}, "timeline": []}
    return {
        "alert": dict(row),
        "timeline": rows_as_dicts(connection.execute("SELECT * FROM alert_timeline WHERE alert_id=? ORDER BY id", (alert_id,)).fetchall()),
    }


def add_timeline(connection, alert_id: int, event_type: str, actor: str, detail: str, created_at: datetime | None = None, from_status: str | None = None, to_status: str | None = None) -> int:
    cursor = connection.execute(
        "INSERT INTO alert_timeline(alert_id,event_type,from_status,to_status,actor,detail,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (alert_id, event_type, from_status, to_status, actor, detail, iso(created_at or utcnow())),
    )
    return cursor.lastrowid


def matching_mute_rule(connection, source: str, severity: str, now: datetime | None = None):
    moment = now or utcnow()
    rows = connection.execute("SELECT * FROM alert_mute_rules WHERE active=1 ORDER BY ends_at DESC").fetchall()
    for rule in rows:
        if parse_time(rule["starts_at"]) <= moment <= parse_time(rule["ends_at"]):
            if fnmatch.fnmatchcase(source.lower(), rule["source_pattern"].lower()) and rule["severity"] in ("*", severity):
                return rule
    return None


def delivery_channels(severity: str, escalation_level: int = 0) -> list[tuple[str, str]]:
    channels = [("In-app", "Ops Command Center")]
    if severity in ("critical", "high") or escalation_level >= 1:
        channels.append(("WhatsApp", "Duty manager group"))
    if severity == "critical" or escalation_level >= 2:
        channels.append(("Email", "Retail operations director"))
    return channels


def deliver_alert(connection, alert_id: int, severity: str, detail: str, now: datetime | None = None, escalation_level: int = 0) -> int:
    moment = now or utcnow()
    delivered = 0
    for channel, recipient in delivery_channels(severity, escalation_level):
        connection.execute(
            "INSERT INTO alert_deliveries(alert_id,channel,recipient,status,attempt,sent_at,detail) VALUES (?, ?, ?, 'delivered', ?, ?, ?)",
            (alert_id, channel, recipient, escalation_level + 1, iso(moment), detail),
        )
        delivered += 1
    return delivered


def create_alert(connection, dedupe_key: str, title: str, message: str, severity: str, source: str, now: datetime | None = None, escalation_minutes: int | None = None) -> dict:
    moment = now or utcnow()
    existing = connection.execute("SELECT * FROM alerts WHERE dedupe_key=? AND status!='resolved' ORDER BY id DESC LIMIT 1", (dedupe_key,)).fetchone()
    if existing:
        return {"id": existing["id"], "created": False, "muted": bool(existing["muted_until"]), "deliveries": 0}
    minutes = escalation_minutes if escalation_minutes is not None else (15 if severity == "critical" else 30 if severity == "high" else 60)
    mute_rule = matching_mute_rule(connection, source, severity, moment)
    muted_until = mute_rule["ends_at"] if mute_rule else None
    escalates_at = muted_until or iso(moment + timedelta(minutes=minutes))
    cursor = connection.execute(
        "INSERT INTO alerts(dedupe_key,title,message,severity,source,status,created_at,escalates_at,muted_until,escalation_level) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, 0)",
        (dedupe_key, title, message, severity, source, iso(moment), escalates_at, muted_until),
    )
    alert_id = cursor.lastrowid
    add_timeline(connection, alert_id, "created", "Rule engine", "Threshold condition created the alert.", moment, None, "open")
    deliveries = 0
    if mute_rule:
        add_timeline(connection, alert_id, "muted", "Mute policy", f"Muted by rule {mute_rule['id']} until {mute_rule['ends_at']}: {mute_rule['reason']}", moment)
    else:
        deliveries = deliver_alert(connection, alert_id, severity, "Initial alert delivery recorded", moment)
        add_timeline(connection, alert_id, "delivery", "Alert engine", f"Recorded {deliveries} initial delivery receipt(s).", moment)
    emit_event(connection, "alert.created", alert_event_payload(connection, alert_id), alert_id=alert_id, now=moment)
    return {"id": alert_id, "created": True, "muted": bool(mute_rule), "deliveries": deliveries}


def transition_alert(connection, alert_id: int, target: str, actor: str = "Ops operator", note: str = "") -> dict:
    alert = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if not alert:
        raise ValueError("Alert not found")
    expected = VALID_TRANSITIONS.get(alert["status"])
    if target != expected:
        raise ValueError(f"Invalid alert transition: {alert['status']} → {target}; next state is {expected or 'none'}")
    moment = utcnow()
    timestamp_column = {"acknowledged": "acknowledged_at", "investigating": "investigating_at", "resolved": "resolved_at"}[target]
    escalates_at = None if target == "resolved" else alert["escalates_at"]
    connection.execute(f"UPDATE alerts SET status=?, {timestamp_column}=?, escalates_at=? WHERE id=?", (target, iso(moment), escalates_at, alert_id))
    add_timeline(connection, alert_id, "transition", actor, note or f"Alert moved to {target}.", moment, alert["status"], target)
    emit_event(connection, "alert.transitioned", dict(alert_event_payload(connection, alert_id), transition={"from": alert["status"], "to": target, "actor": actor}), alert_id=alert_id, now=moment)
    connection.commit()
    return {"alert_id": alert_id, "from": alert["status"], "status": target, "at": iso(moment)}


def create_mute_rule(connection, source_pattern: str, severity: str, duration_minutes: int, reason: str, now: datetime | None = None) -> dict:
    if severity not in ("*", "critical", "high", "medium", "low"):
        raise ValueError("Invalid mute severity")
    if duration_minutes < 1 or duration_minutes > 10080:
        raise ValueError("Mute duration must be between 1 and 10080 minutes")
    moment = now or utcnow()
    cursor = connection.execute(
        "INSERT INTO alert_mute_rules(source_pattern,severity,starts_at,ends_at,reason,active,created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (source_pattern or "*", severity, iso(moment), iso(moment + timedelta(minutes=duration_minutes)), reason.strip() or "Planned operational mute", iso(moment)),
    )
    connection.commit()
    return dict(connection.execute("SELECT * FROM alert_mute_rules WHERE id=?", (cursor.lastrowid,)).fetchone())


def set_mute_rule_active(connection, rule_id: int, active: bool) -> dict:
    updated = connection.execute("UPDATE alert_mute_rules SET active=? WHERE id=?", (int(active), rule_id)).rowcount
    if not updated:
        raise ValueError("Mute rule not found")
    connection.commit()
    return {"id": rule_id, "active": active}


def enforce_escalations(connection, now: datetime | None = None) -> dict:
    moment = now or utcnow()
    alerts = connection.execute("SELECT * FROM alerts WHERE status!='resolved' AND escalates_at IS NOT NULL AND escalates_at<=? ORDER BY escalates_at", (iso(moment),)).fetchall()
    escalated = 0
    muted = 0
    deliveries = 0
    for alert in alerts:
        rule = matching_mute_rule(connection, alert["source"], alert["severity"], moment)
        explicit_mute = parse_time(alert["muted_until"])
        if rule or (explicit_mute and explicit_mute > moment):
            until = rule["ends_at"] if rule else alert["muted_until"]
            connection.execute("UPDATE alerts SET escalates_at=?, muted_until=? WHERE id=?", (until, until, alert["id"]))
            add_timeline(connection, alert["id"], "mute_enforced", "Scheduler", f"Escalation held until {until}.", moment)
            muted += 1
            continue
        level = int(alert["escalation_level"] or 0) + 1
        sent = deliver_alert(connection, alert["id"], alert["severity"], f"Escalation level {level} delivery", moment, level)
        next_minutes = 15 if alert["severity"] == "critical" else 30 if alert["severity"] == "high" else 60
        connection.execute("UPDATE alerts SET escalation_level=?, escalates_at=?, muted_until=NULL WHERE id=?", (level, iso(moment + timedelta(minutes=next_minutes)), alert["id"]))
        add_timeline(connection, alert["id"], "escalated", "Scheduler", f"Escalation level {level}; {sent} delivery receipt(s) recorded.", moment)
        emit_event(connection, "alert.escalated", dict(alert_event_payload(connection, alert["id"]), escalation={"level": level, "deliveries": sent}), alert_id=alert["id"], now=moment)
        escalated += 1
        deliveries += sent
    connection.commit()
    return {"due": len(alerts), "escalated": escalated, "muted": muted, "deliveries": deliveries}


def alerts_payload(connection) -> dict:
    alerts = rows_as_dicts(connection.execute("SELECT * FROM alerts ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, created_at DESC").fetchall())
    for alert in alerts:
        alert["timeline"] = rows_as_dicts(connection.execute("SELECT * FROM alert_timeline WHERE alert_id=? ORDER BY created_at DESC,id DESC", (alert["id"],)).fetchall())
        alert["next_status"] = VALID_TRANSITIONS.get(alert["status"])
    deliveries = rows_as_dicts(connection.execute("SELECT d.*, a.title alert_title, a.severity FROM alert_deliveries d JOIN alerts a ON a.id=d.alert_id ORDER BY d.sent_at DESC,d.id DESC LIMIT 40").fetchall())
    mutes = rows_as_dicts(connection.execute("SELECT * FROM alert_mute_rules ORDER BY created_at DESC,id DESC").fetchall())
    active_states = {"open", "acknowledged", "investigating"}
    counts = {severity: sum(item["severity"] == severity and item["status"] in active_states for item in alerts) for severity in ("critical", "high", "medium", "low")}
    lifecycle = {status: sum(item["status"] == status for item in alerts) for status in ("open", "acknowledged", "investigating", "resolved")}
    return {
        "alerts": alerts, "deliveries": deliveries, "mutes": mutes, "counts": counts, "lifecycle": lifecycle,
        "policies": [
            {"severity": "Critical", "ack": "15 min", "channels": "In-app → WhatsApp → Email", "owner": "Duty manager"},
            {"severity": "High", "ack": "30 min", "channels": "In-app → WhatsApp", "owner": "Functional lead"},
            {"severity": "Medium", "ack": "60 min", "channels": "In-app", "owner": "Operations queue"},
            {"severity": "Low", "ack": "Next business day", "channels": "In-app digest", "owner": "Source owner"},
        ],
    }
