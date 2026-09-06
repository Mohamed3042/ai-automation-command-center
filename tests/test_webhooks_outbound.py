"""Outbound signed webhooks: the outbox, the worker, backoff, and dead-letters."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from app.alerts import create_alert, transition_alert
from app.db import init_db
from app.engine import run_workflow
from app.errors import ApiError
from app.events import (
    BACKOFF_CAP_SECONDS,
    create_subscription,
    deliver_due,
    deliveries_payload,
    delivery_attempts,
    emit_event,
    event_matches,
    next_delay_seconds,
    retry_delivery,
    set_subscription_active,
    subscriptions_payload,
)
from app.scheduler import RelayScheduler
from app.signing import SIGNATURE_HEADER, SignatureError, verify

MOMENT = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size=None):
        return b"ok"

    def getcode(self):
        return self.status


class Receiver:
    """A local receiver fixture that answers a scripted list of status codes."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        if status >= 400:
            raise HTTPError(request.full_url, status, "scripted failure", {}, None)
        return _Response(status)


class OutboundWebhookTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["RELAYOPS_WEBHOOK_BACKOFF_SECONDS"] = "0"
        self.addCleanup(os.environ.pop, "RELAYOPS_WEBHOOK_BACKOFF_SECONDS", None)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "outbound.db"
        self.connection = init_db(self.db_path, reset=True)
        self.addCleanup(self.connection.close)
        self.subscription = create_subscription(self.connection, "ops-receiver", "http://127.0.0.1:9/hook", "whsec_test", "alert.*")

    def make_alert(self, key="outbound-1", severity="high"):
        alert = create_alert(self.connection, key, "Ledger variance", "Debits and credits disagree.", severity, "Workflow engine", MOMENT, 30)
        self.connection.commit()
        return alert

    # ------------------------------------------------------------------ queueing

    def test_alert_creation_queues_one_outbox_row_per_matching_subscription(self):
        alert = self.make_alert()
        rows = self.connection.execute("SELECT * FROM webhook_outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "alert.created")
        self.assertEqual(rows[0]["alert_id"], alert["id"])
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["type"], "alert.created")
        self.assertEqual(payload["data"]["alert"]["severity"], "high")
        self.assertTrue(payload["data"]["timeline"])

    def test_event_filter_excludes_non_matching_types(self):
        create_subscription(self.connection, "finance", "https://example.test/hook", "whsec_x", "workflow.run.failed")
        self.make_alert()
        names = [row["name"] for row in self.connection.execute("SELECT s.name FROM webhook_outbox o JOIN webhook_subscriptions s ON s.id=o.subscription_id")]
        self.assertEqual(names, ["ops-receiver"])
        self.assertTrue(event_matches("workflow.run.failed", "workflow.run.failed"))
        self.assertTrue(event_matches("alert.escalated", "alert.*"))
        self.assertFalse(event_matches("alert.escalated", "workflow.*"))

    def test_paused_subscription_receives_nothing(self):
        set_subscription_active(self.connection, self.subscription["id"], False)
        self.make_alert()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) total FROM webhook_outbox").fetchone()["total"], 0)

    def test_workflow_terminal_states_are_emitted(self):
        run_workflow(self.connection, 1, "manual")
        create_subscription(self.connection, "runs", "https://example.test/runs", "whsec_r", "workflow.*")
        result = run_workflow(self.connection, 1, "manual")
        rows = self.connection.execute("SELECT event_type FROM webhook_outbox WHERE event_type LIKE 'workflow%'").fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["workflow.run.succeeded"])
        self.assertEqual(result["status"], "success")

    # ------------------------------------------------------------------ delivery

    def test_receiver_answering_500_500_200_is_delivered_on_the_third_attempt(self):
        alert = self.make_alert()
        receiver = Receiver(500, 500, 200)
        first = deliver_due(self.connection, MOMENT, receiver)
        second = deliver_due(self.connection, MOMENT, receiver)
        third = deliver_due(self.connection, MOMENT, receiver)
        self.assertEqual((first["retrying"], second["retrying"], third["delivered"]), (1, 1, 1))
        row = self.connection.execute("SELECT * FROM webhook_outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"], row["last_status_code"]), ("delivered", 3, 200))
        attempts = delivery_attempts(self.connection, row["id"])
        self.assertEqual([item["status"] for item in attempts], ["failed", "failed", "delivered"])
        self.assertEqual([item["attempt"] for item in attempts], [1, 2, 3])
        timeline = self.connection.execute("SELECT * FROM alert_timeline WHERE alert_id=? AND event_type='webhook_delivery'", (alert["id"],)).fetchall()
        self.assertEqual(len(timeline), 1)
        self.assertIn("HTTP 200", timeline[0]["detail"])

    def test_delivery_is_signed_and_bound_to_the_body(self):
        self.make_alert()
        receiver = Receiver(200)
        deliver_due(self.connection, MOMENT, receiver)
        request = receiver.requests[0]
        header = request.get_header(SIGNATURE_HEADER.capitalize()) or request.get_header(SIGNATURE_HEADER)
        self.assertIsNotNone(header, "outbound deliveries must carry the signature header")
        verify("whsec_test", request.data, header, 300, now=MOMENT.timestamp())
        with self.assertRaises(SignatureError):
            verify("whsec_test", request.data + b" ", header, 300, now=MOMENT.timestamp())
        with self.assertRaises(SignatureError):
            verify("whsec_wrong", request.data, header, 300, now=MOMENT.timestamp())
        self.assertEqual(request.get_header("X-relayops-event"), "alert.created")

    def test_exhausted_attempts_dead_letter_and_can_be_retried(self):
        alert = self.make_alert()
        receiver = Receiver(500, 500, 500, 500, 500)
        for _ in range(5):
            deliver_due(self.connection, MOMENT, receiver)
        row = self.connection.execute("SELECT * FROM webhook_outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("dead_letter", 5))
        self.assertEqual(len(delivery_attempts(self.connection, row["id"])), 5)
        dead_letter_note = self.connection.execute("SELECT detail FROM alert_timeline WHERE alert_id=? AND event_type='webhook_delivery'", (alert["id"],)).fetchone()
        self.assertIn("Dead-lettered", dead_letter_note["detail"])
        self.assertEqual(deliver_due(self.connection, MOMENT, Receiver(200))["attempted"], 0, "a dead-lettered event must not retry itself")
        requeued = retry_delivery(self.connection, row["id"], MOMENT)
        self.assertEqual(requeued["status"], "pending")
        deliver_due(self.connection, MOMENT, Receiver(200))
        self.assertEqual(self.connection.execute("SELECT status FROM webhook_outbox WHERE id=?", (row["id"],)).fetchone()["status"], "delivered")

    def test_retry_refuses_an_already_delivered_event(self):
        self.make_alert()
        deliver_due(self.connection, MOMENT, Receiver(200))
        row = self.connection.execute("SELECT * FROM webhook_outbox").fetchone()
        with self.assertRaises(ApiError) as caught:
            retry_delivery(self.connection, row["id"], MOMENT)
        self.assertEqual(caught.exception.code, "conflict")

    def test_connection_error_is_recorded_without_a_status_code(self):
        self.make_alert()

        def refuse(request, timeout=None):
            raise OSError("connection refused")

        deliver_due(self.connection, MOMENT, refuse)
        row = self.connection.execute("SELECT * FROM webhook_outbox").fetchone()
        self.assertEqual(row["status"], "retrying")
        self.assertIsNone(row["last_status_code"])
        self.assertIn("connection refused", row["last_error"])

    def test_a_future_next_attempt_is_not_due_yet(self):
        os.environ["RELAYOPS_WEBHOOK_BACKOFF_SECONDS"] = "60"
        self.make_alert()
        deliver_due(self.connection, MOMENT, Receiver(500))
        self.assertEqual(deliver_due(self.connection, MOMENT, Receiver(200))["attempted"], 0)
        later = deliver_due(self.connection, MOMENT + timedelta(minutes=5), Receiver(200))
        self.assertEqual(later["delivered"], 1)

    # ------------------------------------------------------------------ policy

    def test_backoff_grows_and_is_capped(self):
        delays = [next_delay_seconds(attempt, base=5, jitter=False) for attempt in range(1, 8)]
        self.assertEqual(delays[:5], [5, 10, 20, 40, 80])
        self.assertEqual(delays[-1], BACKOFF_CAP_SECONDS)
        jittered = next_delay_seconds(3, base=5)
        self.assertGreaterEqual(jittered, 20)
        self.assertLessEqual(jittered, 24)

    def test_subscription_validation(self):
        with self.assertRaises(ApiError) as bad_url:
            create_subscription(self.connection, "bad", "ftp://example.test", "s")
        self.assertEqual(bad_url.exception.code, "invalid_payload")
        with self.assertRaises(ApiError) as duplicate:
            create_subscription(self.connection, "ops-receiver", "https://example.test/x", "s")
        self.assertEqual(duplicate.exception.code, "conflict")
        generated = create_subscription(self.connection, "auto-secret", "https://example.test/y", "")
        self.assertTrue(generated["secret"].startswith("whsec_"))

    def test_payloads_report_queue_health(self):
        self.make_alert()
        deliver_due(self.connection, MOMENT, Receiver(200))
        subscriptions = subscriptions_payload(self.connection)
        self.assertEqual(subscriptions[0]["delivered"], 1)
        self.assertEqual(subscriptions[0]["dead_letter"], 0)
        rows, total = deliveries_payload(self.connection)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["subscription"], "ops-receiver")

    def test_transitions_queue_their_own_event(self):
        alert = self.make_alert()
        transition_alert(self.connection, alert["id"], "acknowledged", note="Owned")
        types = [row["event_type"] for row in self.connection.execute("SELECT event_type FROM webhook_outbox ORDER BY id")]
        self.assertEqual(types, ["alert.created", "alert.transitioned"])
        payload = json.loads(self.connection.execute("SELECT payload_json FROM webhook_outbox ORDER BY id DESC LIMIT 1").fetchone()["payload_json"])
        self.assertEqual(payload["data"]["transition"], {"from": "open", "to": "acknowledged", "actor": "Ops operator"})

    def test_scheduler_tick_runs_the_delivery_worker(self):
        self.make_alert()
        scheduler = RelayScheduler(str(self.db_path))
        result = scheduler.tick(MOMENT)
        self.assertIn("deliveries", result)
        self.assertGreaterEqual(result["deliveries"]["attempted"], 1, "the tick must drain the outbox")
        pending = self.connection.execute("SELECT COUNT(*) total FROM webhook_outbox WHERE status='pending'").fetchone()
        self.assertEqual(pending["total"], 0, "every queued event was attempted by the tick")

    def test_emit_event_is_idempotent_per_event_id(self):
        emit_event(self.connection, "alert.created", {"alert": {"id": 1}}, alert_id=None, event_id="evt_fixed", now=MOMENT)
        emit_event(self.connection, "alert.created", {"alert": {"id": 1}}, alert_id=None, event_id="evt_fixed", now=MOMENT)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) total FROM webhook_outbox WHERE event_id='evt_fixed'").fetchone()["total"], 1)


if __name__ == "__main__":
    unittest.main()
