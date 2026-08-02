from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.db import init_db
from server import create_server


class HttpApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "http.db"
        init_db(self.db_path, reset=True).close()
        self.server = create_server("127.0.0.1", 0, self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_health_exposes_scheduler_and_provider(self):
        status, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "1.1.0")
        self.assertIn("scheduler", body)
        self.assertIn("fallback_available", body["llm"])

    def test_workflow_run_accepts_structured_failure_injection(self):
        status, body = self.request("/api/workflows/1/run", {"failure_injection": {"action": "transform.map", "fail_attempts": 1}})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["retries"], 1)

    def test_builder_create_and_update_routes(self):
        payload = {"name": "HTTP workflow", "description": "Created through the public builder API.", "owner": "QA", "trigger_type": "manual", "active": True, "steps": [{"name": "Refresh", "action": "metrics.increment", "config": {}, "retry_limit": 1, "manual_minutes": 2, "estimate_basis": "per_run"}]}
        created_status, created = self.request("/api/workflows", payload)
        payload["name"] = "HTTP workflow updated"
        updated_status, updated = self.request(f"/api/workflows/{created['id']}", payload, method="PUT")
        self.assertEqual((created_status, updated_status), (201, 200))
        self.assertEqual(updated["name"], "HTTP workflow updated")

    def test_workflow_toggle_route(self):
        status, body = self.request("/api/workflows/1/toggle", {"active": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["active"])

    def test_alert_transition_and_mute_routes(self):
        mute_status, mute = self.request("/api/alerts/mutes", {"source_pattern": "Test*", "severity": "*", "duration_minutes": 30, "reason": "API test"})
        transition_status, transition = self.request("/api/alerts/1/transition", {"status": "acknowledged", "note": "Owned"})
        self.assertEqual((mute_status, transition_status), (201, 200))
        self.assertTrue(mute["active"])
        self.assertEqual(transition["status"], "acknowledged")

    def test_invalid_builder_payload_returns_400(self):
        status, body = self.request("/api/workflows", {"name": "Broken", "description": "No steps", "steps": []})
        self.assertEqual(status, 400)
        self.assertIn("At least one", body["error"])

    def test_scheduler_payload_and_manual_tick_routes(self):
        status, payload = self.request("/api/scheduler")
        tick_status, tick = self.request("/api/scheduler/tick", {"now": "2026-08-02T09:45:00Z"})
        self.assertEqual((status, tick_status), (200, 201))
        self.assertEqual(len(payload["jobs"]), 5)
        self.assertIn("escalations", tick)

    def test_static_app_contains_workflow_builder_navigation(self):
        request = Request(self.base + "/")
        with urlopen(request, timeout=5) as response:
            html = response.read().decode()
        self.assertIn("Workflow Builder", html)


if __name__ == "__main__":
    unittest.main()
