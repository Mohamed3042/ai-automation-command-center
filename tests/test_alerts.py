from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.alerts import alerts_payload, create_alert, create_mute_rule, enforce_escalations, matching_mute_rule, set_mute_rule_active, transition_alert
from app.db import init_db


UTC = timezone.utc


class AlertLifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = init_db(Path(self.tempdir.name) / "alerts.db", reset=True)

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def test_strict_lifecycle_records_all_operator_transitions(self):
        first = transition_alert(self.connection, 1, "acknowledged", note="Owned by shift lead")
        second = transition_alert(self.connection, 1, "investigating", note="Promotion feed inspected")
        third = transition_alert(self.connection, 1, "resolved", note="Expected campaign uplift")
        self.assertEqual((first["status"], second["status"], third["status"]), ("acknowledged", "investigating", "resolved"))
        alert = self.connection.execute("SELECT * FROM alerts WHERE id=1").fetchone()
        self.assertTrue(alert["acknowledged_at"] and alert["investigating_at"] and alert["resolved_at"])
        self.assertIsNone(alert["escalates_at"])

    def test_lifecycle_cannot_skip_states(self):
        with self.assertRaisesRegex(ValueError, "next state is acknowledged"):
            transition_alert(self.connection, 1, "resolved")

    def test_initial_delivery_channels_follow_severity(self):
        moment = datetime(2026, 8, 3, tzinfo=UTC)
        low = create_alert(self.connection, "low-new", "Low", "Detail", "low", "Unit test", moment)
        critical = create_alert(self.connection, "critical-new", "Critical", "Detail", "critical", "Unit test", moment)
        self.assertEqual(low["deliveries"], 1)
        self.assertEqual(critical["deliveries"], 3)

    def test_active_alert_deduplication_and_resolved_reopening(self):
        moment = datetime(2026, 8, 3, tzinfo=UTC)
        first = create_alert(self.connection, "repeatable", "A", "B", "medium", "Test", moment)
        duplicate = create_alert(self.connection, "repeatable", "A", "B", "medium", "Test", moment)
        self.assertTrue(first["created"])
        self.assertFalse(duplicate["created"])
        for status in ("acknowledged", "investigating", "resolved"):
            transition_alert(self.connection, first["id"], status)
        reopened = create_alert(self.connection, "repeatable", "A again", "B", "medium", "Test", moment + timedelta(hours=1))
        self.assertTrue(reopened["created"])
        self.assertNotEqual(reopened["id"], first["id"])

    def test_mute_rule_matching_supports_patterns_and_severity(self):
        moment = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        create_mute_rule(self.connection, "Inventory*", "high", 60, "Stocktake", moment)
        self.assertIsNotNone(matching_mute_rule(self.connection, "Inventory rules", "high", moment))
        self.assertIsNone(matching_mute_rule(self.connection, "Inventory rules", "critical", moment))

    def test_muted_alert_stays_visible_without_delivery(self):
        moment = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        create_mute_rule(self.connection, "Inventory*", "*", 60, "Stocktake", moment)
        created = create_alert(self.connection, "muted-new", "Muted", "Still visible", "high", "Inventory rules", moment)
        self.assertTrue(created["muted"])
        self.assertEqual(created["deliveries"], 0)
        self.assertIsNotNone(self.connection.execute("SELECT id FROM alerts WHERE id=?", (created["id"],)).fetchone())

    def test_escalation_increments_level_and_adds_timeline(self):
        moment = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        created = create_alert(self.connection, "due-new", "Due", "Escalate", "high", "Test", moment, escalation_minutes=1)
        result = enforce_escalations(self.connection, moment + timedelta(minutes=2))
        alert = self.connection.execute("SELECT escalation_level FROM alerts WHERE id=?", (created["id"],)).fetchone()
        self.assertEqual(alert["escalation_level"], 1)
        self.assertGreaterEqual(result["deliveries"], 2)
        self.assertIsNotNone(self.connection.execute("SELECT id FROM alert_timeline WHERE alert_id=? AND event_type='escalated'", (created["id"],)).fetchone())

    def test_resolved_alert_is_never_escalated(self):
        for status in ("acknowledged", "investigating", "resolved"):
            transition_alert(self.connection, 1, status)
        before = self.connection.execute("SELECT COUNT(*) n FROM alert_deliveries WHERE alert_id=1").fetchone()["n"]
        enforce_escalations(self.connection, datetime(2026, 8, 4, tzinfo=UTC))
        after = self.connection.execute("SELECT COUNT(*) n FROM alert_deliveries WHERE alert_id=1").fetchone()["n"]
        self.assertEqual(after, before)

    def test_mute_rule_can_be_disabled(self):
        rule = create_mute_rule(self.connection, "*", "*", 30, "Pause")
        result = set_mute_rule_active(self.connection, rule["id"], False)
        self.assertFalse(result["active"])

    def test_payload_exposes_lifecycle_and_timeline(self):
        payload = alerts_payload(self.connection)
        self.assertEqual(set(payload["lifecycle"]), {"open", "acknowledged", "investigating", "resolved"})
        self.assertTrue(all("timeline" in alert and "next_status" in alert for alert in payload["alerts"]))


if __name__ == "__main__":
    unittest.main()
