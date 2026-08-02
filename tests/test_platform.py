from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.dashboard import alerts_payload, connectors_payload, dashboard_payload
from app.db import init_db
from app.engine import evaluate_alert_rules, run_workflow, workflows_payload
from app.intelligence import LLMAdapter, detect_sales_anomalies, demand_forecast, support_intelligence
from app.reports import generate_report


class PlatformTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        self.connection = init_db(self.db_path, reset=True)

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def test_seed_contains_all_required_system_domains(self):
        payload = connectors_payload(self.connection)
        self.assertEqual(payload["metrics"]["connected"], 6)
        self.assertEqual(
            {item["category"] for item in payload["connectors"]},
            {"Point of sale", "E-commerce", "Accounting", "Email", "Messaging", "Spreadsheets"},
        )
        self.assertEqual(payload["metrics"]["healthy"], 5)

    def test_dashboard_metrics_and_drill_down_reconcile(self):
        all_stores = dashboard_payload(self.connection, 7)
        online = dashboard_payload(self.connection, 7, "Online")
        self.assertEqual(len(all_stores["timeline"]), 7)
        self.assertGreater(all_stores["kpis"][0]["value"], online["kpis"][0]["value"])
        self.assertGreater(online["kpis"][1]["value"], 0)
        self.assertEqual(online["scope"]["store"], "Online")

    def test_workflow_chains_persist_success_and_failure_paths(self):
        success = run_workflow(self.connection, 1)
        failure = run_workflow(self.connection, 4, force_error=True)
        self.assertEqual(success["status"], "success")
        self.assertEqual(len(success["steps"]), 4)
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["retries"], 3)
        self.assertTrue(any(step["status"] == "skipped" for step in failure["steps"]))
        self.assertIsNotNone(self.connection.execute("SELECT id FROM alerts WHERE dedupe_key=?", (f"workflow-run-{failure['id']}",)).fetchone())

    def test_ai_layer_is_deterministic_and_key_free(self):
        adapter = LLMAdapter()
        first = adapter.classify("Charged twice", "My card shows a duplicate checkout payment")
        second = adapter.classify("Charged twice", "My card shows a duplicate checkout payment")
        self.assertEqual(first, second)
        self.assertEqual(first["category"], "Payment")
        self.assertEqual(first["adapter"], "Deterministic offline fallback")
        anomalies = detect_sales_anomalies(self.connection)
        self.assertTrue(any(item["store"] == "Downtown Flagship" and item["category"] == "Electronics" for item in anomalies))
        forecast = demand_forecast(self.connection)
        self.assertEqual(len(forecast["points"]), 14)
        self.assertGreater(forecast["projected_revenue"], 0)
        self.assertGreaterEqual(support_intelligence(self.connection)["metrics"]["automation_rate"], 80)

    def test_report_scheduler_writes_real_html_and_csv(self):
        output_dir = Path(self.tempdir.name) / "reports"
        with patch("app.reports.REPORT_DIR", output_dir):
            artifacts = generate_report(self.connection, "weekly")
        self.assertEqual({item["format"] for item in artifacts}, {"HTML", "CSV"})
        for artifact in artifacts:
            output = output_dir / artifact["path"]
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 200)
        html_artifact = next(item for item in artifacts if item["format"] == "HTML")
        self.assertIn("Weekly Operations Review", (output_dir / html_artifact["path"]).read_text())

    def test_alert_engine_deduplicates_and_records_deliveries(self):
        first = evaluate_alert_rules(self.connection)
        second = evaluate_alert_rules(self.connection)
        self.assertGreater(first["alerts_created"], 0)
        self.assertGreater(first["deliveries_recorded"], first["alerts_created"])
        self.assertEqual(second["alerts_created"], 0)
        payload = alerts_payload(self.connection)
        self.assertGreater(len(payload["deliveries"]), 0)

    def test_workflow_catalog_exposes_steps_and_run_history(self):
        payload = workflows_payload(self.connection)
        self.assertEqual(payload["metrics"]["active"], 4)
        self.assertTrue(all(len(item["steps"]) == 4 for item in payload["workflows"]))
        self.assertTrue(any(run["status"] == "failed" for run in payload["recent_runs"]))


if __name__ == "__main__":
    unittest.main()
