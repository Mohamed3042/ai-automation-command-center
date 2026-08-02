from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import init_db
from app.scheduler import RelayScheduler, cron_matches, next_cron_run, scheduler_payload


UTC = timezone.utc


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "scheduler.db"
        self.report_patch = patch("app.reports.REPORT_DIR", Path(self.tempdir.name) / "reports")
        self.report_patch.start()
        self.connection = init_db(self.db_path, reset=True)
        self.connection.execute("UPDATE workflows SET next_run_at='2026-08-04T00:00:00Z' WHERE trigger_type='cron'")
        self.connection.execute("UPDATE report_schedules SET next_run='2026-08-04T00:00:00Z'")
        self.connection.commit()
        self.scheduler = RelayScheduler(self.db_path, tick_seconds=0.1)

    def tearDown(self):
        if self.scheduler.running:
            self.scheduler.stop()
        self.connection.close()
        self.report_patch.stop()
        self.tempdir.cleanup()

    def test_cron_matches_steps_ranges_and_lists(self):
        moment = datetime(2026, 8, 3, 8, 30, tzinfo=UTC)
        self.assertTrue(cron_matches(moment, "*/15 8 1-10 8 1,3"))
        self.assertFalse(cron_matches(moment, "10 8 * * *"))

    def test_cron_day_of_month_and_weekday_use_standard_or_semantics(self):
        monday = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
        self.assertTrue(cron_matches(monday, "0 8 10 * 1"))

    def test_invalid_cron_is_rejected(self):
        with self.assertRaises(ValueError):
            cron_matches(datetime.now(UTC), "61 * * * *")
        with self.assertRaises(ValueError):
            cron_matches(datetime.now(UTC), "* * *")

    def test_next_cron_run_is_strictly_after_reference(self):
        reference = datetime(2026, 8, 2, 23, 30, tzinfo=UTC)
        self.assertEqual(next_cron_run("30 23 * * *", reference), datetime(2026, 8, 3, 23, 30, tzinfo=UTC))

    def test_due_workflow_is_fired_and_advanced(self):
        moment = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        self.connection.execute("UPDATE workflows SET next_run_at=? WHERE id=3", (moment.isoformat().replace("+00:00", "Z"),))
        self.connection.commit()
        result = self.scheduler.tick(moment)
        self.assertEqual([(item["type"], item["id"]) for item in result["fired"]], [("workflow", 3)])
        event = self.connection.execute("SELECT * FROM scheduler_events WHERE job_type='workflow' AND job_id=3").fetchone()
        self.assertEqual(event["status"], "success")
        self.assertGreater(self.connection.execute("SELECT COUNT(*) n FROM workflow_runs WHERE trigger_type='schedule'").fetchone()["n"], 0)

    def test_due_report_schedule_writes_artifacts_and_event(self):
        moment = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)
        self.connection.execute("UPDATE report_schedules SET next_run=? WHERE id=1", (moment.isoformat().replace("+00:00", "Z"),))
        self.connection.commit()
        result = self.scheduler.tick(moment)
        self.assertEqual([(item["type"], item["id"]) for item in result["fired"]], [("report", 1)])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM reports").fetchone()["n"], 2)

    def test_future_jobs_do_not_fire(self):
        result = self.scheduler.tick(datetime(2026, 8, 3, 1, 0, tzinfo=UTC))
        self.assertEqual(result["fired"], [])

    def test_scheduler_enforces_overdue_alert_escalation(self):
        moment = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
        before = self.connection.execute("SELECT escalation_level FROM alerts WHERE id=1").fetchone()["escalation_level"]
        result = self.scheduler.tick(moment)
        after = self.connection.execute("SELECT escalation_level FROM alerts WHERE id=1").fetchone()["escalation_level"]
        self.assertEqual(after, before + 1)
        self.assertGreater(result["escalations"]["deliveries"], 0)

    def test_start_and_stop_publish_thread_state(self):
        self.scheduler.start()
        self.assertTrue(self.scheduler.running)
        self.scheduler.stop()
        state = self.connection.execute("SELECT status FROM scheduler_state WHERE id=1").fetchone()["status"]
        self.assertEqual(state, "stopped")

    def test_payload_surfaces_next_runs_and_events(self):
        self.scheduler.tick(datetime(2026, 8, 3, 1, 0, tzinfo=UTC))
        payload = scheduler_payload(self.connection)
        self.assertEqual(len(payload["jobs"]), 5)
        self.assertTrue(all(job["next_run"] for job in payload["jobs"]))


if __name__ == "__main__":
    unittest.main()
