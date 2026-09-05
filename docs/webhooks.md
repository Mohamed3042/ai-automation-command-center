# Webhooks

RelayOps signs and verifies with one scheme in both directions (ADR
[0003](adr/0003-webhook-signing.md)).

```
X-RelayOps-Signature: t=1754130271,v1=6f1c8b...
v1 = HMAC_SHA256(secret, "<t>.<raw request body>")
```

`t` is Unix seconds. The signed string is the timestamp, a full stop, and **the
exact request body bytes** — not a re-serialised object, not a pretty-printed copy.

## Verify a delivery from RelayOps

Fifteen lines, standard library, any language. Python:

```python
import hashlib, hmac, time

def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    timestamp, signatures = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = int(value)
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    if abs(time.time() - timestamp) > tolerance:      # a captured request expires
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)
```

`integrations/receiver/receiver.py` is a runnable receiver built on exactly this,
and `docker compose --profile receiver up` starts it on port 4999.

## Send a signed webhook to RelayOps

```bash
SECRET=whsec_relayops_dev_orders                      # development value; set RELAYOPS_WEBHOOK_SECRET in a deployment
BODY='{"id":"SF-90001","created_at":"2026-08-02T10:14:00Z","currency":"USD","total_price":"184.50","financial_status":"paid","location":{"name":"Riverside Flagship"}}'
T=$(date +%s)
SIG=$(printf '%s' "$T.$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)

curl -sS -X POST http://localhost:4173/api/v1/webhooks/orders \
  -H "Content-Type: application/json" \
  -H "X-RelayOps-Signature: t=$T,v1=$SIG" \
  -H "Idempotency-Key: demo-1" \
  -d "$BODY"
```

The response is `202` with the receipt and the id of the automation run that
processed the order.

## Inbound endpoints

| Path | Source | Becomes | Runs |
|---|---|---|---|
| `POST /api/v1/webhooks/orders` | storefront | a staged order | *Omnichannel order sync* |
| `POST /api/v1/webhooks/tickets` | helpdesk | an unclassified ticket | *Support triage and routing* |

The order mapper accepts the aliases a storefront actually sends: `order_id` or
`id` or `name` or `order_number`; `store` or `store_name` or `location.name`;
`amount` or `total_price` or `total`; `status` or `financial_status`. A payload it
cannot map is a `422` that names the missing fields.

## Failure modes and what they mean

| HTTP | `error.code` | Cause |
|---|---|---|
| 401 | `signature_missing` | no `X-RelayOps-Signature` header |
| 401 | `signature_malformed` | the header has no `t=` or no `v1=`, or `t` is not an integer |
| 401 | `signature_expired` | `t` is outside the tolerance window — usually a clock, not a key |
| 401 | `signature_invalid` | wrong secret, or the body changed after signing |
| 409 | `conflict` | the external id is already staged under a different idempotency key |
| 422 | `unprocessable` | the payload cannot be mapped; `details.missing` lists the fields |
| 202 | — | accepted; a replay returns the first receipt with `"duplicate": true` |

## Outbound: subscribe to RelayOps events

```bash
curl -sS -X POST http://localhost:4173/api/v1/webhooks/subscriptions \
  -H "Authorization: Bearer $RELAYOPS_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"ops-receiver","url":"https://receiver.example/hook","event_filter":"alert.*"}'
```

The response carries the signing secret **once**. Event types:

| Event | When |
|---|---|
| `alert.created` | a rule or a failed run raised an alert |
| `alert.transitioned` | an operator or an integration advanced the lifecycle |
| `alert.escalated` | the scheduler escalated an overdue alert |
| `workflow.run.succeeded` / `workflow.run.failed` | a run reached a terminal state |

`event_filter` is space- or comma-separated fnmatch patterns (`alert.*`,
`workflow.run.failed`, `*`).

The body is `{"id", "type", "created_at", "data"}`; alert events carry the alert
row and its full timeline, run events carry the run and its workflow.

## Delivery policy

Five attempts. The delay before attempt *n* is `base * 2^(n-1)` seconds with up to
20 % jitter, capped at 300 s; `base` is `RELAYOPS_WEBHOOK_BACKOFF_SECONDS`
(default 5). Anything outside `2xx` is a failure. After the fifth attempt the event
becomes `dead_letter` — visible on the Webhooks screen with a **Retry now** button,
never silently dropped.

Every attempt is stored with its status code and duration, and an alert-linked
delivery writes its outcome into that alert's immutable timeline:

```
webhook_delivery | Webhook worker | Delivered alert.created to n8n-alert-escalation (HTTP 200, attempt 1).
```
