#!/usr/bin/env python3
"""Exercise the running platform through its public HTTP boundary."""
from __future__ import annotations

import json
import sys
from urllib.request import Request, urlopen


BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:4173"


def request(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


health = request("/api/health")
dashboard = request("/api/dashboard?days=7")
connectors = request("/api/connectors")
intelligence = request("/api/intelligence")
success_run = request("/api/workflows/1/run", {})
failure_run = request("/api/workflows/4/run", {"force_error": True})
alert_result = request("/api/alerts/evaluate", {})
daily_files = request("/api/reports/generate", {"report_type": "daily"})
weekly_files = request("/api/reports/generate", {"report_type": "weekly"})

assert health["status"] == "ok"
assert connectors["metrics"]["connected"] == 6
assert len(dashboard["timeline"]) == 7
assert len(intelligence["forecast"]["points"]) == 14
assert intelligence["adapter"]["external_keys_required"] is False
assert success_run["status"] == "success"
assert failure_run["status"] == "failed" and failure_run["retries"] == 3
assert len(daily_files["artifacts"]) == 2
assert len(weekly_files["artifacts"]) == 2

print("LIVE VERIFICATION PASSED")
print(f"  systems connected: {connectors['metrics']['connected']}")
print(f"  7-day net revenue: ${dashboard['kpis'][0]['value']:,.0f}")
print(f"  workflow success: {success_run['run_key']} ({success_run['records_processed']} records)")
print(f"  failure handling: {failure_run['retries']} retries, alert escalation persisted")
print(f"  anomaly signals: {len(intelligence['anomalies'])}")
print(f"  forecast horizon: {len(intelligence['forecast']['points'])} days")
print(f"  support auto-routing: {intelligence['support']['metrics']['automation_rate']}%")
print(f"  rule evaluation: {alert_result['rules_evaluated']} rules, {alert_result['alerts_created']} new alerts")
print("  report artifacts: daily HTML/CSV + weekly HTML/CSV")
