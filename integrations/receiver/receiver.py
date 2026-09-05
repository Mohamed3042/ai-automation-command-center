#!/usr/bin/env python3
"""A minimal signed-webhook receiver, for demonstrating outbound delivery.

Standard library only, like RelayOps itself. It verifies the RelayOps signature
with the shared secret, prints one JSON line per delivery, and can be told to
fail the first N attempts so a backoff and dead-letter can be watched happening.

    RECEIVER_SECRET=whsec_demo RECEIVER_FAIL_FIRST=2 python receiver.py

Environment:
    RECEIVER_PORT        port to listen on (default 4999)
    RECEIVER_SECRET      signing secret; unset means signatures are not checked
    RECEIVER_FAIL_FIRST  answer 500 to the first N deliveries (default 0)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RECEIVER_PORT", "4999"))
SECRET = os.environ.get("RECEIVER_SECRET", "")
FAIL_FIRST = int(os.environ.get("RECEIVER_FAIL_FIRST", "0"))
TOLERANCE = int(os.environ.get("RECEIVER_TOLERANCE", "300"))
_SEEN = {"count": 0}


def verify(secret: str, body: bytes, header: str) -> str:
    timestamp = None
    signatures = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return "malformed"
    if abs(time.time() - timestamp) > TOLERANCE:
        return "expired"
    expected = hmac.new(secret.encode(), "{0}.".format(timestamp).encode() + body, hashlib.sha256).hexdigest()
    return "ok" if any(hmac.compare_digest(expected, item) for item in signatures) else "invalid"


class Handler(BaseHTTPRequestHandler):
    server_version = "RelayOpsWebhookReceiver/1.0"

    def log_message(self, fmt, *args):
        return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"status": "ok", "deliveries": _SEEN["count"], "fail_first": FAIL_FIRST})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        _SEEN["count"] += 1
        signature_state = verify(SECRET, raw, self.headers.get("X-RelayOps-Signature", "")) if SECRET else "unchecked"
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            event = {}
        record = {
            "delivery": _SEEN["count"],
            "event_type": event.get("type") or self.headers.get("X-RelayOps-Event"),
            "event_id": event.get("id"),
            "attempt": self.headers.get("X-RelayOps-Delivery-Attempt"),
            "signature": signature_state,
        }
        if SECRET and signature_state != "ok":
            record["result"] = "rejected"
            print(json.dumps(record), flush=True)
            self._reply(401, {"error": "signature {0}".format(signature_state)})
            return
        if _SEEN["count"] <= FAIL_FIRST:
            record["result"] = "forced_failure"
            print(json.dumps(record), flush=True)
            self._reply(500, {"error": "forced failure {0} of {1}".format(_SEEN["count"], FAIL_FIRST)})
            return
        record["result"] = "accepted"
        print(json.dumps(record), flush=True)
        self._reply(200, {"received": True, "event_id": event.get("id")})


if __name__ == "__main__":
    print(json.dumps({"event": "receiver.started", "port": PORT, "verifying": bool(SECRET), "fail_first": FAIL_FIRST}), flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
