# 0003 - One HMAC-SHA256 signing scheme, in both directions

Date: 2026-09-05 - Status: accepted

## Context

RelayOps needed to accept webhooks from a storefront and a helpdesk, and to send
webhooks to subscribers. Inbound and outbound could have used different schemes, and
a shared-secret header would have been the least work.

A bare shared secret in a header is replayable forever and says nothing about the
body. It also would not interoperate with the sibling ATMPL project, which already
signs with a timestamped HMAC in the same shape.

## Decision

One scheme, `app/signing.py`, used in both directions:

```
X-RelayOps-Signature: t=<unix seconds>,v1=<hex>
v1 = HMAC_SHA256(secret, "<t>.<raw request body>")
```

- The signature covers the **exact bytes on the wire**, never a re-serialised object.
  The n8n Code node builds the JSON string and the HTTP node posts that same string
  for this reason.
- `t` must be inside the source's tolerance window (300 s by default), which is what
  makes a captured request unusable later.
- Comparison is constant-time, and several `v1=` values are accepted so a secret can
  be rotated without a flag day.
- Inbound secrets resolve environment-first (`RELAYOPS_WEBHOOK_SECRET_<SOURCE>`, then
  `RELAYOPS_WEBHOOK_SECRET`, then the seeded development value). The seeded value is
  published in this repository on purpose, so a fresh clone can sign its own test
  request; `.env.example` says in as many words that a deployment sets the variable.
- Outbound, each subscription owns its secret, returned once at creation.

Idempotency is a separate concern from authenticity: the `Idempotency-Key` header, or
the payload's own external id, is unique per source, so a replay returns the first
receipt and creates nothing.

## Consequences

- A caller can be written in fifteen lines in any language; `docs/webhooks.md` shows
  the verifier.
- RelayOps and ATMPL can call each other without a translation layer.
- A wrong secret, a tampered body and a stale timestamp are distinct error codes
  (`signature_invalid` and `signature_expired`), so an integrator can tell a clock
  problem from a key problem.
- The tolerance window means a caller's clock matters. That is a real operational
  constraint, and it is documented rather than absorbed.
