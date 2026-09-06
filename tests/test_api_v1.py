"""The versioned API surface: keys, scopes, envelope, pagination, request ids."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.api.keys import authenticate, create_key, revoke_key
from app.api.core import ApiError
from app.db import init_db
from server import create_server


class ApiV1TestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "api.db"
        self.connection = init_db(self.db_path, reset=True)
        self.full = create_key(self.connection, "full-access", "*")["token"]
        self.reader = create_key(self.connection, "read-only", "workflows:read runs:read")["token"]
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

    def call(self, path, payload=None, method=None, token=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", "Bearer {0}".format(token))
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read()
                body = json.loads(raw) if response.headers.get("Content-Type", "").startswith("application/json") else raw.decode()
                return response.status, body, dict(response.headers)
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode()
            return exc.code, body, dict(exc.headers)

    # ------------------------------------------------------------------ auth

    def test_missing_key_is_rejected_with_the_shared_envelope(self):
        status, body, headers = self.call("/api/v1/workflows")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertTrue(body["error"]["request_id"].startswith("req_"))
        self.assertEqual(headers["X-Request-Id"], body["error"]["request_id"])

    def test_unknown_and_malformed_tokens_are_rejected(self):
        self.assertEqual(self.call("/api/v1/workflows", token="relay_deadbeef_nope")[0], 401)
        self.assertEqual(self.call("/api/v1/workflows", token="not-a-relayops-token")[0], 401)

    def test_scope_is_enforced_per_route(self):
        status, body, _ = self.call("/api/v1/reports", token=self.reader)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertIn("reports:read", body["error"]["message"])
        self.assertEqual(self.call("/api/v1/workflows", token=self.reader)[0], 200)

    def test_revoked_key_stops_working(self):
        token = create_key(self.connection, "temporary", "workflows:read")["token"]
        self.assertEqual(self.call("/api/v1/workflows", token=token)[0], 200)
        revoke_key(self.connection, token.split("_")[1])
        status, body, _ = self.call("/api/v1/workflows", token=token)
        self.assertEqual(status, 401)
        self.assertIn("revoked", body["error"]["message"])

    def test_secret_is_hashed_at_rest_and_last_use_is_tracked(self):
        row = self.connection.execute("SELECT * FROM api_keys WHERE name='full-access'").fetchone()
        secret = self.full.split("_", 2)[2]
        self.assertNotIn(secret, row["secret_hash"])
        self.assertEqual(len(row["secret_hash"]), 64)
        self.call("/api/v1/workflows", token=self.full)
        refreshed = self.connection.execute("SELECT last_used_at FROM api_keys WHERE name='full-access'").fetchone()
        self.assertIsNotNone(refreshed["last_used_at"])

    def test_unknown_scope_is_refused_at_mint_time(self):
        with self.assertRaises(ApiError):
            create_key(self.connection, "bad", "workflows:teleport")

    # ------------------------------------------------------------------ routing

    def test_public_routes_need_no_key(self):
        status, body, _ = self.call("/api/v1/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertIn("slack", body)
        ready_status, ready_body, _ = self.call("/api/v1/ready")
        self.assertIn(ready_status, (200, 503))
        self.assertIn("database", ready_body["checks"])

    def test_unknown_route_and_wrong_method_use_the_envelope(self):
        status, body, _ = self.call("/api/v1/nope", token=self.full)
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        wrong_status, wrong_body, _ = self.call("/api/v1/workflows", payload={}, token=self.full)
        self.assertEqual((wrong_status, wrong_body["error"]["code"]), (405, "method_not_allowed"))
        self.assertEqual(wrong_body["error"]["details"]["allow"], ["GET"])

    def test_pagination_envelope_is_consistent(self):
        status, body, _ = self.call("/api/v1/workflows?limit=2&offset=0", token=self.full)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["pagination"]["limit"], 2)
        self.assertEqual(body["pagination"]["total"], 4)
        self.assertEqual(body["pagination"]["next_offset"], 2)
        last = self.call("/api/v1/workflows?limit=2&offset=2", token=self.full)[1]
        self.assertIsNone(last["pagination"]["next_offset"])

    def test_invalid_pagination_is_a_400(self):
        status, body, _ = self.call("/api/v1/workflows?limit=0", token=self.full)
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"))
        self.assertEqual(self.call("/api/v1/workflows?limit=abc", token=self.full)[0], 400)

    def test_run_and_read_back_through_the_api(self):
        status, run, _ = self.call("/api/v1/workflows/1/run", payload={}, method="POST", token=self.full)
        self.assertEqual((status, run["status"]), (201, "success"))
        listed = self.call("/api/v1/runs?limit=5", token=self.full)[1]
        self.assertEqual(listed["data"][0]["id"], run["id"])
        detail = self.call("/api/v1/runs/{0}".format(run["id"]), token=self.full)[1]
        self.assertEqual(len(detail["attempts"]), 4)
        attempts = self.call("/api/v1/runs/{0}/attempts".format(run["id"]), token=self.full)[1]
        self.assertEqual(attempts["pagination"]["total"], 4)

    def test_filters_narrow_list_routes(self):
        self.call("/api/v1/workflows/1/run", payload={}, method="POST", token=self.full)
        only_success = self.call("/api/v1/runs?status=success", token=self.full)[1]
        self.assertTrue(only_success["data"])
        self.assertTrue(all(item["status"] == "success" for item in only_success["data"]))
        self.assertEqual(self.call("/api/v1/runs?status=failed", token=self.full)[1]["data"], [])

    def test_missing_resources_are_404_not_500(self):
        self.assertEqual(self.call("/api/v1/workflows/9999", token=self.full)[0], 404)
        self.assertEqual(self.call("/api/v1/runs/9999", token=self.full)[0], 404)
        self.assertEqual(self.call("/api/v1/alerts/9999", token=self.full)[0], 404)

    def test_metrics_are_prometheus_text(self):
        self.call("/api/v1/workflows", token=self.full)
        status, body, headers = self.call("/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn("# TYPE relayops_http_requests_total counter", body)
        self.assertIn("relayops_alerts{state=", body)
        self.assertIn("relayops_process_uptime_seconds", body)

    def test_openapi_document_and_docs_page_are_served(self):
        status, body, headers = self.call("/api/v1/openapi.yaml")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/yaml"))
        self.assertIn("openapi: 3.1.0", body)
        docs_status, docs_body, docs_headers = self.call("/api/v1/docs")
        self.assertEqual(docs_status, 200)
        self.assertTrue(docs_headers["Content-Type"].startswith("text/html"))
        self.assertIn("redoc.standalone.js", docs_body)

    def test_security_headers_and_request_id_are_present_on_every_response(self):
        for path in ("/api/v1/health", "/api/v1/workflows"):
            headers = self.call(path, token=self.full)[2]
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertTrue(headers["X-Request-Id"].startswith("req_"))

    def test_legacy_v11_surface_is_unchanged_and_unauthenticated(self):
        status, body, _ = self.call("/api/workflows")
        self.assertEqual(status, 200)
        self.assertIn("workflows", body)
        self.assertNotIn("pagination", body)

    def test_authenticate_helper_rejects_a_tampered_secret(self):
        key_id, secret = self.full.split("_")[1], self.full.split("_", 2)[2]
        with self.assertRaises(ApiError):
            authenticate(self.connection, "Bearer relay_{0}_{1}".format(key_id, secret[:-1] + "x"))


if __name__ == "__main__":
    unittest.main()
