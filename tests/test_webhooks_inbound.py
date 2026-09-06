"""Inbound webhooks: signature, tolerance, idempotency, and the run they start."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.db import init_db
from app.signing import SignatureError, compute_signature, signature_header, verify
from app.webhooks import map_order, resolve_secret
from server import create_server

ORDER = {
    "id": "SF-90001",
    "created_at": "2026-08-02T10:14:00Z",
    "currency": "USD",
    "total_price": "184.50",
    "financial_status": "paid",
    "location": {"name": "Riverside Flagship"},
}
TICKET = {
    "ticket_id": "HD-90001",
    "customer": "Dana Ellis",
    "subject": "Charged twice for order 90001",
    "body": "My card shows two charges for the same order and I was charged twice.",
    "channel": "Email",
}


class InboundWebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "inbound.db"
        self.connection = init_db(self.db_path, reset=True)
        self.order_secret = resolve_secret(self.connection, "orders")
        self.ticket_secret = resolve_secret(self.connection, "tickets")
        self.server = create_server("127.0.0.1", 0, self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{0}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.connection.close()
        self.tempdir.cleanup()

    def post(self, path, payload, secret=None, timestamp=None, signature=None, idempotency_key=None, body=None):
        raw = body if body is not None else json.dumps(payload).encode()
        header = signature
        if header is None:
            header = signature_header(secret, raw, timestamp)
        request = Request(self.base + path, data=raw, method="POST", headers={"Content-Type": "application/json", "X-RelayOps-Signature": header})
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    # ------------------------------------------------------------------ happy path

    def test_signed_order_is_accepted_staged_and_run(self):
        status, body = self.post("/api/v1/webhooks/orders", ORDER, self.order_secret)
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "processed")
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["external_id"], "SF-90001")
        self.assertIsNotNone(body["run_id"])
        staged = self.connection.execute("SELECT * FROM staged_orders WHERE external_id='SF-90001'").fetchone()
        self.assertEqual(staged["status"], "normalized")
        canonical = self.connection.execute("SELECT * FROM canonical_orders WHERE external_id='SF-90001'").fetchone()
        self.assertEqual(canonical["store"], "Riverside Flagship")
        self.assertAlmostEqual(canonical["amount"], 184.50, places=2)
        run = self.connection.execute("SELECT * FROM workflow_runs WHERE id=?", (body["run_id"],)).fetchone()
        self.assertEqual((run["status"], run["trigger_type"]), ("success", "webhook"))
        ledger = self.connection.execute("SELECT COUNT(*) total FROM ledger_entries WHERE canonical_order_id=?", (canonical["id"],)).fetchone()
        self.assertEqual(ledger["total"], 2)

    def test_signed_ticket_is_classified_and_routed(self):
        status, body = self.post("/api/v1/webhooks/tickets", TICKET, self.ticket_secret)
        self.assertEqual((status, body["status"]), (202, "processed"))
        ticket = self.connection.execute("SELECT * FROM support_tickets WHERE subject=?", (TICKET["subject"],)).fetchone()
        self.assertNotEqual(ticket["category"], "Unclassified")
        self.assertIsNotNone(ticket["routed_queue"])
        self.assertEqual(ticket["priority"], "urgent")

    # ------------------------------------------------------------------ rejection

    def test_tampered_body_is_rejected_and_stages_nothing(self):
        raw = json.dumps(ORDER).encode()
        header = signature_header(self.order_secret, raw)
        tampered = json.dumps(dict(ORDER, total_price="999999.00")).encode()
        status, body = self.post("/api/v1/webhooks/orders", None, signature=header, body=tampered)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "signature_invalid")
        self.assertIsNone(self.connection.execute("SELECT id FROM staged_orders WHERE external_id='SF-90001'").fetchone())

    def test_stale_timestamp_is_rejected(self):
        status, body = self.post("/api/v1/webhooks/orders", ORDER, self.order_secret, timestamp=int(time.time()) - 4000)
        self.assertEqual((status, body["error"]["code"]), (401, "signature_expired"))

    def test_missing_and_malformed_signatures_are_rejected(self):
        self.assertEqual(self.post("/api/v1/webhooks/orders", ORDER, signature="")[1]["error"]["code"], "signature_missing")
        self.assertEqual(self.post("/api/v1/webhooks/orders", ORDER, signature="v1=abc")[1]["error"]["code"], "signature_malformed")
        self.assertEqual(self.post("/api/v1/webhooks/orders", ORDER, signature="t=notanint,v1=abc")[1]["error"]["code"], "signature_malformed")

    def test_wrong_secret_is_rejected(self):
        status, body = self.post("/api/v1/webhooks/orders", ORDER, "whsec_wrong_secret")
        self.assertEqual((status, body["error"]["code"]), (401, "signature_invalid"))

    def test_unmappable_payload_is_422_with_the_missing_fields(self):
        status, body = self.post("/api/v1/webhooks/orders", {"id": "SF-1", "created_at": "2026-08-02T10:00:00Z"}, self.order_secret)
        self.assertEqual((status, body["error"]["code"]), (422, "unprocessable"))
        self.assertEqual(body["error"]["details"]["missing"], ["store", "amount"])

    def test_zero_amount_order_is_refused(self):
        payload = dict(ORDER, id="SF-90002", total_price="0.00")
        status, body = self.post("/api/v1/webhooks/orders", payload, self.order_secret)
        self.assertEqual((status, body["error"]["code"]), (422, "unprocessable"))

    # ------------------------------------------------------------------ idempotency

    def test_replay_returns_the_first_receipt_and_creates_nothing(self):
        first_status, first = self.post("/api/v1/webhooks/orders", ORDER, self.order_secret, idempotency_key="delivery-1")
        second_status, second = self.post("/api/v1/webhooks/orders", ORDER, self.order_secret, idempotency_key="delivery-1")
        self.assertEqual((first_status, second_status), (202, 202))
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["replays"], 1)
        staged = self.connection.execute("SELECT COUNT(*) total FROM staged_orders WHERE external_id='SF-90001'").fetchone()
        self.assertEqual(staged["total"], 1)
        receipts = self.connection.execute("SELECT COUNT(*) total FROM webhook_receipts").fetchone()
        self.assertEqual(receipts["total"], 1)

    def test_idempotency_defaults_to_the_payload_external_id(self):
        self.post("/api/v1/webhooks/orders", ORDER, self.order_secret)
        status, body = self.post("/api/v1/webhooks/orders", ORDER, self.order_secret)
        self.assertEqual(status, 202)
        self.assertTrue(body["duplicate"])

    def test_a_different_order_under_a_new_key_is_processed(self):
        self.post("/api/v1/webhooks/orders", ORDER, self.order_secret)
        second = dict(ORDER, id="SF-90003")
        status, body = self.post("/api/v1/webhooks/orders", second, self.order_secret)
        self.assertEqual((status, body["duplicate"]), (202, False))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) total FROM webhook_receipts").fetchone()["total"], 2)

    # ------------------------------------------------------------------ surfaces

    def test_receipts_and_sources_are_readable_through_the_api(self):
        from app.api.keys import create_key

        token = create_key(self.connection, "reader", "webhooks:read")["token"]
        self.post("/api/v1/webhooks/orders", ORDER, self.order_secret)
        request = Request(self.base + "/api/v1/webhooks/receipts", headers={"Authorization": "Bearer {0}".format(token)})
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertEqual(payload["data"][0]["source_slug"], "orders")
        sources_request = Request(self.base + "/api/v1/webhooks/sources", headers={"Authorization": "Bearer {0}".format(token)})
        with urlopen(sources_request, timeout=10) as response:
            sources = json.load(response)
        self.assertEqual(sorted(item["slug"] for item in sources["data"]), ["orders", "tickets"])
        self.assertIn("seeded", sources["data"][0]["secret_source"])


class SigningTestCase(unittest.TestCase):
    def test_signature_is_over_timestamp_and_exact_body(self):
        body = b'{"a":1}'
        header = signature_header("secret", body, 1754130271)
        self.assertEqual(header, "t=1754130271,v1={0}".format(compute_signature("secret", 1754130271, body)))
        self.assertEqual(verify("secret", body, header, 300, now=1754130271), 1754130271)

    def test_verification_is_bound_to_the_body(self):
        header = signature_header("secret", b'{"a":1}', 1754130271)
        with self.assertRaises(SignatureError) as caught:
            verify("secret", b'{"a":2}', header, 300, now=1754130271)
        self.assertEqual(caught.exception.code, "signature_invalid")

    def test_tolerance_window_is_symmetric(self):
        header = signature_header("secret", b"{}", 1_000_000)
        verify("secret", b"{}", header, 300, now=1_000_299)
        verify("secret", b"{}", header, 300, now=999_701)
        with self.assertRaises(SignatureError) as caught:
            verify("secret", b"{}", header, 300, now=1_000_400)
        self.assertEqual(caught.exception.code, "signature_expired")

    def test_order_mapper_accepts_the_documented_aliases(self):
        mapped = map_order({"order_number": 10041, "store_name": "Harbour East", "total": 42.5, "processed_at": "2026-08-02T09:00:00Z"})
        self.assertEqual(mapped["canonical"]["order_id"], "10041")
        self.assertEqual(mapped["canonical"]["store"], "Harbour East")
        self.assertEqual(mapped["canonical"]["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
