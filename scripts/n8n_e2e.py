#!/usr/bin/env python3
"""Assert that the three n8n workflows really drive RelayOps.

Run against a live `docker compose up` stack (CI does exactly this):

    python3 scripts/n8n_e2e.py --relayops http://localhost:4173 --n8n http://localhost:5678 --api-key relay_...

It sends one storefront order at n8n's production webhook and proves the order
reached RelayOps through a valid signature; then it forces a workflow failure so
RelayOps emits an alert, and proves the outbound delivery reached n8n and came
back as a Slack connector receipt. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ORDER = {
    "id": "N8N-E2E-{0}".format(int(time.time())),
    "created_at": "2026-08-02T11:05:00Z",
    "currency": "USD",
    "total_price": "321.75",
    "financial_status": "paid",
    "location": {"name": "Harbour East"},
}


def call(url, payload=None, method=None, token=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", "Bearer {0}".format(token))
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}
    except URLError as exc:
        return 0, {"error": str(exc)}


def wait_for(description, predicate, attempts=60, delay=1.0):
    for attempt in range(1, attempts + 1):
        result = predicate()
        if result:
            print("  ok   {0} (after {1}s)".format(description, attempt - 1))
            return result
        time.sleep(delay)
    raise SystemExit("FAILED: timed out waiting for {0}".format(description))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relayops", default="http://localhost:4173")
    parser.add_argument("--n8n", default="http://localhost:5678")
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()
    relayops = args.relayops.rstrip("/")
    n8n = args.n8n.rstrip("/")
    token = args.api_key

    print("1. both services answer")
    wait_for("RelayOps is ready", lambda: call(relayops + "/api/v1/ready")[0] == 200)
    wait_for("n8n is healthy", lambda: call(n8n + "/healthz")[0] == 200)

    print("2. an order posted at n8n reaches RelayOps with a valid signature")
    status, receipt = call(n8n + "/webhook/relayops-order", ORDER, "POST", timeout=60)
    if status != 200:
        raise SystemExit("FAILED: n8n webhook returned {0}: {1}".format(status, receipt))
    if receipt.get("status") != "processed":
        raise SystemExit("FAILED: RelayOps did not process the order: {0}".format(receipt))
    print("  ok   receipt {0} -> run {1} ({2} row results)".format(receipt["receipt_id"], receipt["run_id"], receipt["result"]["run"]["records_processed"]))

    stored = call(relayops + "/api/v1/webhooks/receipts?limit=20", token=token)[1]
    match = [item for item in stored.get("data", []) if item["external_id"] == ORDER["id"]]
    if not match:
        raise SystemExit("FAILED: the receipt is not in the RelayOps store")
    run_status, run = call(relayops + "/api/v1/runs/{0}".format(match[0]["run_id"]), token=token)
    if run_status != 200 or run["status"] != "success":
        raise SystemExit("FAILED: the order automation run is not a success: {0}".format(run))
    print("  ok   run {0} status {1}, trigger {2}".format(run["id"], run["status"], run["trigger_type"]))

    print("3. a RelayOps alert escalates through n8n to the Slack connector")
    failure = {"failure_injection": {"action": "accounting.reconcile", "fail_attempts": 10, "message": "n8n e2e: ledger adapter unavailable"}}
    status, result = call(relayops + "/api/workflows/4/run", failure, "POST", timeout=60)
    if status != 201 or result.get("status") != "failed" or not result.get("alert"):
        raise SystemExit("FAILED: the forced failure did not create an alert: {0}".format(result))
    alert_id = result["alert"]["id"]
    print("  ok   alert {0} created by the failed run {1}".format(alert_id, result["id"]))

    def delivered():
        payload = call(relayops + "/api/v1/webhooks/deliveries?limit=50", token=token)[1]
        return [item for item in payload.get("data", []) if item["alert_id"] == alert_id and item["status"] == "delivered"]

    deliveries = wait_for("the alert event is delivered to n8n", delivered)
    print("  ok   {0} delivered to {1} (HTTP {2})".format(deliveries[0]["event_type"], deliveries[0]["subscription"], deliveries[0]["last_status_code"]))

    def slack_receipt():
        payload = call(relayops + "/api/v1/connectors/messages?limit=50", token=token)[1]
        return [item for item in payload.get("data", []) if item["alert_id"] == alert_id and item["connector_slug"] == "slack"]

    receipts = wait_for("n8n calls back into the Slack connector", slack_receipt)
    print("  ok   slack receipt {0} in {1} mode: {2}".format(receipts[0]["id"], receipts[0]["mode"], receipts[0]["detail"][:70]))

    timeline = call(relayops + "/api/v1/alerts/{0}".format(alert_id), token=token)[1]
    events = [item["event_type"] for item in timeline["timeline"]]
    if "webhook_delivery" not in events:
        raise SystemExit("FAILED: the delivery receipt is not in the alert timeline: {0}".format(events))
    print("  ok   alert timeline carries the delivery receipt; alert is now {0}".format(timeline["status"]))

    print("\nN8N END-TO-END PASSED")
    print("  order {0} -> receipt {1} -> run {2}".format(ORDER["id"], receipt["receipt_id"], receipt["run_id"]))
    print("  alert {0} -> {1} outbound deliver(y/ies) -> slack {2} receipt".format(alert_id, len(deliveries), receipts[0]["mode"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
