from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.intelligence as intelligence
from app.db import init_db
from app.engine import hours_saved_metrics, run_workflow, save_workflow, toggle_workflow, workflows_payload
from app.intelligence import LLMAdapter, intelligence_payload


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class BuilderAndProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = init_db(Path(self.tempdir.name) / "builder.db", reset=True)
        intelligence._PROVIDER_STATE.update({"active": "offline-rules", "last_error": None, "last_used_at": None})

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def payload(self):
        return {
            "name": "Morning KPI checkpoint",
            "description": "Refreshes the governed KPI cache on an operator-defined schedule.",
            "owner": "Operations analytics",
            "active": True,
            "trigger_type": "cron",
            "schedule_cron": "15 6 * * *",
            "steps": [
                {"name": "Refresh cache", "action": "metrics.increment", "config": {"scope": "all"}, "retry_limit": 2, "retry_backoff_ms": 10, "manual_minutes": 4, "estimate_basis": "per_run"},
                {"name": "Render summary", "action": "report.render", "config": {"type": "daily"}, "retry_limit": 1, "retry_backoff_ms": 5, "manual_minutes": 8, "estimate_basis": "per_run"},
            ],
        }

    def test_builder_creates_ordered_persisted_workflow(self):
        saved = save_workflow(self.connection, self.payload())
        self.assertEqual(saved["name"], "Morning KPI checkpoint")
        self.assertEqual([step["position"] for step in saved["steps"]], [1, 2])
        self.assertEqual([step["action"] for step in saved["steps"]], ["metrics.increment", "report.render"])

    def test_cron_builder_calculates_a_real_next_run(self):
        saved = save_workflow(self.connection, self.payload())
        self.assertTrue(saved["next_run_at"].endswith("Z"))
        self.assertEqual(saved["next_run_at"][14:16], "15")

    def test_builder_edits_and_reorders_existing_steps(self):
        saved = save_workflow(self.connection, self.payload())
        update = self.payload()
        update["name"] = "Reordered checkpoint"
        update["steps"] = [dict(saved["steps"][1]), dict(saved["steps"][0])]
        updated = save_workflow(self.connection, update, saved["id"])
        self.assertEqual(updated["name"], "Reordered checkpoint")
        self.assertEqual([step["action"] for step in updated["steps"]], ["report.render", "metrics.increment"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM workflow_steps WHERE workflow_id=? AND active=1", (saved["id"],)).fetchone()["n"], 2)

    def test_builder_rejects_unknown_action_and_bad_cron(self):
        payload = self.payload()
        payload["steps"][0]["action"] = "fiction.execute"
        with self.assertRaisesRegex(ValueError, "unknown action"):
            save_workflow(self.connection, payload)
        payload = self.payload()
        payload["schedule_cron"] = "61 * * * *"
        with self.assertRaises(ValueError):
            save_workflow(self.connection, payload)

    def test_builder_toggle_persists(self):
        toggle_workflow(self.connection, 1, False)
        workflow = next(item for item in workflows_payload(self.connection)["workflows"] if item["id"] == 1)
        self.assertFalse(workflow["active"])

    def test_builder_estimate_edits_do_not_restate_historical_hours(self):
        run_workflow(self.connection, 1)
        before = hours_saved_metrics(self.connection)["minutes"]
        workflow = next(item for item in workflows_payload(self.connection)["workflows"] if item["id"] == 1)
        workflow["steps"][0]["manual_minutes"] = 100
        save_workflow(self.connection, workflow, 1)
        self.assertEqual(hours_saved_metrics(self.connection)["minutes"], before)

    def test_v1_seed_runs_are_preserved_but_excluded_from_observed_evidence(self):
        self.connection.execute("UPDATE metadata SET value='1.0.0' WHERE key='seed_version'")
        self.connection.execute("UPDATE workflow_steps SET manual_minutes=0 WHERE id=1")
        self.connection.execute("INSERT INTO workflow_runs(workflow_id,run_key,status,trigger_type,started_at,duration_ms,records_processed,retries) VALUES (1,'legacy-run','success','seed','2026-08-02T08:00:00Z',120,50,0)")
        self.connection.commit()
        self.connection.close()
        self.connection = init_db(Path(self.tempdir.name) / "builder.db")
        run = self.connection.execute("SELECT evidence_source FROM workflow_runs WHERE run_key='legacy-run'").fetchone()
        step = self.connection.execute("SELECT manual_minutes FROM workflow_steps WHERE id=1").fetchone()
        self.assertEqual(run["evidence_source"], "legacy_seed")
        self.assertEqual(step["manual_minutes"], 0.1)
        self.assertEqual(workflows_payload(self.connection)["metrics"]["runs_30d"], 0)

    def test_offline_adapter_is_deterministic_without_key(self):
        adapter = LLMAdapter(provider="offline-rules", api_key="")
        first = adapter.classify("Charged twice", "Duplicate card payment")
        second = adapter.classify("Charged twice", "Duplicate card payment")
        self.assertEqual(first, second)
        self.assertEqual(first["adapter"], "Deterministic offline fallback")
        self.assertFalse(adapter.status()["configured"])

    def test_hosted_adapter_uses_openai_compatible_response(self):
        observed = {}

        def opener(request, timeout):
            observed["authorization"] = request.headers["Authorization"]
            observed["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": '{"category":"Delivery","priority":"high","confidence":0.94}'}}]})

        adapter = LLMAdapter(api_key="secret", endpoint="https://provider.example/v1/chat/completions", model="model-x", opener=opener)
        result = adapter.classify("Late parcel", "Tracking has not moved")
        self.assertEqual(result["category"], "Delivery")
        self.assertEqual(result["adapter"], "Hosted provider · model-x")
        self.assertEqual(observed["authorization"], "Bearer secret")
        self.assertEqual(adapter.status()["active"], "hosted-openai-compatible")

    def test_hosted_failure_falls_back_and_surfaces_status(self):
        def opener(request, timeout):
            raise TimeoutError("provider unavailable")

        adapter = LLMAdapter(api_key="secret", opener=opener)
        result = adapter.classify("Reset password", "Email never arrived")
        status = adapter.status()
        self.assertEqual(result["adapter"], "Deterministic offline fallback")
        self.assertEqual(status["active"], "offline-rules")
        self.assertIn("provider unavailable", status["last_error"])
        self.assertTrue(status["fallback_available"])

    def test_intelligence_payload_exposes_provider_contract(self):
        status = intelligence_payload(self.connection)["adapter"]
        self.assertIn("configured", status)
        self.assertIn("fallback_available", status)
        self.assertFalse(status["external_keys_required"])


if __name__ == "__main__":
    unittest.main()
