#!/usr/bin/env python3
"""Exercise RelayOps v1.1 through its public HTTP boundary."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:4173"


def request(path: str, payload: dict | None = None, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


health = request("/api/health")
dashboard = request("/api/dashboard?days=7")
connectors = request("/api/connectors")
initial_workflows = request("/api/workflows")

order_run = request("/api/workflows/1/run", {})
support_run = request("/api/workflows/2/run", {})
inventory_run = request("/api/workflows/3/run", {})
retry_run = request("/api/workflows/4/run", {"failure_injection": {"action": "accounting.reconcile", "fail_attempts": 1, "message": "Live verifier transient fault"}})
failure_run = request("/api/workflows/1/run", {"failure_injection": {"action": "transform.map", "fail_attempts": 10, "message": "Live verifier terminal fault"}})

failure_alert_id = failure_run["alert"]["id"]
for status in ("acknowledged", "investigating", "resolved"):
    transition = request(f"/api/alerts/{failure_alert_id}/transition", {"status": status, "note": f"Live verifier: {status}"})
    assert transition["status"] == status

mute = request("/api/alerts/mutes", {"source_pattern": "Inventory*", "severity": "high", "duration_minutes": 60, "reason": "Live verifier maintenance window"})

builder_payload = {
    "name": "Morning KPI checkpoint",
    "description": "Refreshes the governed KPI cache through a persisted operator-built workflow.",
    "owner": "Operations analytics",
    "active": True,
    "trigger_type": "manual",
    "steps": [
        {"name": "Refresh KPI cache", "action": "metrics.increment", "config": {"scope": "all"}, "retry_limit": 2, "retry_backoff_ms": 5, "manual_minutes": 4, "estimate_basis": "per_run"},
        {"name": "Aggregate trading totals", "action": "sales.aggregate", "config": {"window": "day"}, "retry_limit": 1, "retry_backoff_ms": 5, "manual_minutes": 12, "estimate_basis": "per_run"},
    ],
}
created_workflow = request("/api/workflows", builder_payload)
builder_payload["steps"] = list(reversed(created_workflow["steps"]))
updated_workflow = request(f"/api/workflows/{created_workflow['id']}", builder_payload, method="PUT")
paused_workflow = request(f"/api/workflows/{created_workflow['id']}/toggle", {"active": False})
enabled_workflow = request(f"/api/workflows/{created_workflow['id']}/toggle", {"active": True})
builder_run = request(f"/api/workflows/{created_workflow['id']}/run", {})

rule_result = request("/api/alerts/evaluate", {})
# Tick far enough ahead that every seeded schedule is due, whatever today is: eight
# days covers the */30 workflow, the daily close, the weekday pack and the Monday
# review. A literal date here rots - once real time passes it, the background
# scheduler has already advanced every next-run and a tick then fires nothing.
tick_at = (datetime.now(timezone.utc) + timedelta(days=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
scheduler_tick = request("/api/scheduler/tick", {"now": tick_at})
daily_files = request("/api/reports/generate", {"report_type": "daily"})
weekly_files = request("/api/reports/generate", {"report_type": "weekly"})

workflows = request("/api/workflows")
intelligence = request("/api/intelligence")
alerts = request("/api/alerts")
scheduler = request("/api/scheduler")
reports = request("/api/reports")

assert health["status"] == "ok" and health["version"] == "1.2.0"
assert health["scheduler"] == "running"
assert connectors["metrics"]["connected"] == 6
assert len(dashboard["timeline"]) == 7
assert initial_workflows["metrics"]["hours_saved"]["minutes"] >= 0
assert order_run["status"] == "success" and order_run["records_processed"] >= 27
assert next(step for step in order_run["steps"] if step["action"] == "accounting.post")["output"]["rows_written"] == 24
assert support_run["status"] == "success" and support_run["records_processed"] == 16
assert inventory_run["status"] == "success"
assert retry_run["status"] == "success" and retry_run["retries"] == 1
assert failure_run["status"] == "failed" and failure_run["retries"] == 2
assert len([step for step in failure_run["steps"] if step["action"] == "transform.map"]) == 3
assert any(step["status"] == "skipped" for step in failure_run["steps"])
assert mute["active"] == 1
assert [step["action"] for step in updated_workflow["steps"]] == ["sales.aggregate", "metrics.increment"]
assert paused_workflow["active"] is False and enabled_workflow["active"] is True
assert builder_run["status"] == "success"
assert len(scheduler_tick["fired"]) == 5, f"a tick eight days ahead must fire all five seeded jobs, fired {scheduler_tick['fired']}"
assert scheduler["state"]["jobs_fired"] >= len(scheduler_tick["fired"])
assert all(datetime.fromisoformat(job["next_run"].replace("Z", "+00:00")) > datetime.fromisoformat(tick_at.replace("Z", "+00:00")) for job in scheduler["jobs"]), "every fired job must have advanced past the tick"
assert all(job["next_run"] for job in scheduler["jobs"])
assert intelligence["adapter"]["fallback_available"] is True
assert intelligence["support"]["metrics"]["automation_rate"] >= 80
assert alerts["lifecycle"]["resolved"] >= 1
assert any(item["id"] == failure_alert_id and len(item["timeline"]) >= 5 for item in alerts["alerts"])
assert workflows["metrics"]["hours_saved"]["minutes"] > 0
assert len(daily_files["artifacts"]) == 2 and len(weekly_files["artifacts"]) == 2
assert reports["metrics"]["generated_30d"] >= 4

print("LIVE VERIFICATION PASSED")
print(f"  real order pipeline: {order_run['records_processed']} row results; 12 orders / 24 ledger writes")
print(f"  support classification: {support_run['records_processed']} row results; {intelligence['support']['metrics']['automation_rate']}% auto-routing")
print(f"  retry evidence: transient={retry_run['retries']} retry; terminal={failure_run['retries']} retries + downstream skips")
print(f"  workflow builder: created, reordered, toggled, and ran workflow {created_workflow['id']}")
print(f"  scheduler: tick at {tick_at} fired {len(scheduler_tick['fired'])} due jobs; {len(scheduler['jobs'])} next runs advanced")
print(f"  alert lifecycle: failure alert {failure_alert_id} acknowledged -> investigating -> resolved; mute {mute['id']} active")
print(f"  time returned: {workflows['metrics']['hours_saved']['minutes']} calculated minutes from observed volume")
print(f"  report vault: {reports['metrics']['generated_30d']} persisted artifacts")
