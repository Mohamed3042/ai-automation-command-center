from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import init_db
from app.engine import FailureInjector, hours_saved_metrics, run_workflow, toggle_workflow


class TruthfulEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "engine.db"
        self.report_patch = patch("app.reports.REPORT_DIR", Path(self.tempdir.name) / "reports")
        self.report_patch.start()
        self.connection = init_db(self.db_path, reset=True)

    def tearDown(self):
        self.connection.close()
        self.report_patch.stop()
        self.tempdir.cleanup()

    def test_order_workflow_writes_canonical_and_balanced_rows(self):
        result = run_workflow(self.connection, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM canonical_orders").fetchone()["n"], 12)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM ledger_entries").fetchone()["n"], 24)
        balance = self.connection.execute("SELECT SUM(debit) debit,SUM(credit) credit FROM ledger_entries").fetchone()
        self.assertEqual(balance["debit"], balance["credit"])

    def test_order_workflow_is_idempotent_after_ingestion(self):
        run_workflow(self.connection, 1)
        second = run_workflow(self.connection, 1)
        self.assertEqual(second["status"], "success")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM canonical_orders").fetchone()["n"], 12)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM ledger_entries").fetchone()["n"], 24)
        self.assertEqual(second["steps"][0]["records_processed"], 0)

    def test_support_workflow_classifies_and_routes_unclassified_rows(self):
        result = run_workflow(self.connection, 2)
        self.assertEqual(result["records_processed"], 16)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM support_tickets WHERE category='Unclassified'").fetchone()["n"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM support_tickets WHERE classified_at IS NOT NULL AND routed_queue IS NOT NULL").fetchone()["n"], 4)

    def test_inventory_workflow_creates_tasks_and_deliveries(self):
        run_workflow(self.connection, 3)
        tasks = self.connection.execute("SELECT COUNT(*) n FROM replenishment_tasks").fetchone()["n"]
        messages = self.connection.execute("SELECT COUNT(*) n FROM outbound_messages WHERE channel='WhatsApp'").fetchone()["n"]
        self.assertGreater(tasks, 0)
        self.assertEqual(messages, tasks)

    def test_finance_close_uses_posted_ledger_and_generates_artifacts(self):
        run_workflow(self.connection, 1)
        result = run_workflow(self.connection, 4)
        reconciliation = next(step for step in result["steps"] if step["action"] == "accounting.reconcile")
        self.assertTrue(reconciliation["output"]["balanced"])
        self.assertEqual(reconciliation["records_processed"], 24)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"], 2)

    def test_transient_failure_runs_real_retry_then_succeeds(self):
        result = run_workflow(self.connection, 1, failure_injector=FailureInjector(action="transform.map", fail_attempts=1))
        attempts = [step for step in result["steps"] if step["action"] == "transform.map"]
        self.assertEqual([step["status"] for step in attempts], ["retrying", "success"])
        self.assertEqual(result["retries"], 1)
        self.assertTrue(all(step["duration_ms"] > 0 for step in attempts))

    def test_terminal_failure_exhausts_configured_attempts(self):
        result = run_workflow(self.connection, 1, failure_injector=FailureInjector(action="transform.map", fail_attempts=10))
        attempts = [step for step in result["steps"] if step["action"] == "transform.map"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result["retries"], 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM canonical_orders").fetchone()["n"], 0)

    def test_downstream_skips_are_persisted_after_terminal_failure(self):
        result = run_workflow(self.connection, 1, failure_injector=FailureInjector(action="webhook.validate", fail_attempts=10))
        self.assertEqual(sum(step["status"] == "skipped" for step in result["steps"]), 3)
        persisted = self.connection.execute("SELECT COUNT(*) n FROM workflow_run_steps WHERE run_id=? AND status='skipped'", (result["id"],)).fetchone()["n"]
        self.assertEqual(persisted, 3)

    def test_hours_saved_is_derived_from_successful_step_volumes(self):
        self.assertEqual(hours_saved_metrics(self.connection)["minutes"], 0)
        run_workflow(self.connection, 1)
        saved = hours_saved_metrics(self.connection)
        self.assertEqual(saved["minutes"], 16.6)
        self.assertIn("actual successful records", saved["formula"])

    def test_paused_workflow_rejects_schedule_but_allows_operator_run(self):
        toggle_workflow(self.connection, 1, False)
        with self.assertRaisesRegex(ValueError, "paused"):
            run_workflow(self.connection, 1, trigger_type="schedule")
        self.assertEqual(run_workflow(self.connection, 1)["status"], "success")


if __name__ == "__main__":
    unittest.main()
