# 0004 - n8n is the integration layer; the outbox lives inside RelayOps

Date: 2026-09-05 - Status: accepted

## Context

"RelayOps talks to the outside" could mean two very different systems: RelayOps grows
a connector for every vendor, or RelayOps exposes a clean edge and a workflow tool
does the vendor-specific wiring.

Separately, outbound delivery needed a home. A queue (Redis, RabbitMQ, Celery) would
be the usual answer and would end the standard-library runtime.

## Decision

**n8n owns the vendor wiring.** Three workflows live in `integrations/n8n/`, exported
from a real n8n instance and re-imported by CI: order intake, alert escalation to
Slack, and a nightly report drop. Anything vendor-shaped - mapping a storefront
payload, choosing a Slack channel, writing to a shared folder - is a node in n8n, not
a branch in RelayOps.

**RelayOps owns the edge and the outbox.** The transactional outbox
(`webhook_subscriptions`, `webhook_outbox`, `webhook_delivery_attempts`) is written in
the same SQLite transaction as the business change, and the delivery worker runs
inside the existing scheduler tick: five attempts, exponential backoff with jitter
capped at 300 s, then `dead_letter`, with every attempt recorded and an alert-linked
delivery writing its receipt into that alert's immutable timeline.

## Consequences

- No broker, no second process, no new dependency; the scheduler tick already holds a
  lock, so deliveries cannot overlap.
- Delivery latency is bounded by the tick (1 s), far below anything this system needs.
- A workflow change is a JSON diff a reviewer can read, and CI proves it still runs.
- The traps this cost are written down in `integrations/n8n/README.md`: importing
  overwrites the active flag, a named volume is created root-owned, n8n refuses to
  write inside its own data directory, and `n8n execute` needs a callable trigger.
- If RelayOps ever needs horizontal scaling, the outbox is already the right shape and
  only the worker's placement changes. **[INFERRED]**
