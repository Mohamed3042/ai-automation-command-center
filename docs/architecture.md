# RelayOps architecture

RelayOps is a retail operations control plane that runs as **one Python process on
SQLite with no third-party runtime dependency**. v1.2 keeps that promise and adds
an integration surface: a versioned REST API, signed webhooks in both directions,
a Slack connector, and three n8n workflows that make RelayOps part of a wider
automation estate.

Honesty labels used below: **VERIFIED** (measured on a running system),
**[INFERRED]** (a design call, not a measurement).

## C4 level 1 — system context

```mermaid
flowchart LR
    storefront["Storefront<br/>(order source)"]
    helpdesk["Helpdesk<br/>(ticket source)"]
    operator["Retail operations<br/>operator"]
    client["API client<br/>(scoped key)"]
    slack["Slack<br/>(incoming webhook)"]
    receiver["Subscriber endpoint<br/>(any signed receiver)"]

    subgraph estate["Automation estate"]
      n8n["n8n<br/>3 workflows"]
    end

    relayops["RelayOps<br/>workflows · alerts · reports · API"]

    storefront -- "order JSON" --> n8n
    helpdesk -- "ticket JSON" --> n8n
    n8n -- "signed POST /api/v1/webhooks/*" --> relayops
    relayops -- "signed alert + run events" --> n8n
    n8n -- "Block Kit + acknowledge" --> relayops
    relayops -- "Block Kit message" --> slack
    relayops -- "signed events" --> receiver
    operator -- "browser UI" --> relayops
    client -- "Bearer relay_..." --> relayops
```

The storefront and helpdesk in this repository are **synthetic**: the demo data is
seeded and every screen says so. The n8n workflows, the signatures, the API and
the delivery machinery are real and exercised by CI. **VERIFIED** — the CI job
`n8n-e2e` imports the workflows into a fresh n8n container, fires an order at its
production webhook, and asserts the order reached RelayOps and the escalation came
back as a Slack connector receipt.

## C4 level 2 — containers

```mermaid
flowchart TB
    subgraph compose["docker compose"]
      subgraph relayops["relayops (python:3.12-slim, non-root, HEALTHCHECK)"]
        http["ThreadingHTTPServer<br/>request id · JSON access log"]
        api["app/api<br/>route table · scopes · envelope"]
        legacy["/api/* (v1.1)<br/>same-origin UI surface"]
        inbound["app/webhooks.py<br/>verify · map · idempotency"]
        engine["app/engine.py<br/>savepoints · retries · clocks"]
        alerts["app/alerts.py<br/>lifecycle · mutes · escalation"]
        reports["app/reports.py<br/>HTML + CSV"]
        sched["app/scheduler.py<br/>cron tick + delivery worker"]
        outbox["app/events.py<br/>subscriptions · outbox · backoff"]
        connectors["app/connectors/slack.py<br/>live or fixture"]
        metrics["app/metrics.py<br/>/metrics"]
      end
      db[("SQLite + WAL<br/>/data volume")]
      files[("Report vault<br/>/app/data/reports")]
      n8n["n8n 2.37.10<br/>order intake · alert escalation · nightly report"]
      receiver["receiver (profile)<br/>signed-webhook fixture"]
    end

    http --> api --> engine
    http --> legacy --> engine
    api --> inbound --> engine
    engine --> db
    engine --> alerts --> db
    alerts --> outbox --> db
    sched --> engine
    sched --> alerts
    sched --> outbox
    sched --> reports --> files
    outbox -- "signed POST" --> n8n
    outbox -- "signed POST" --> receiver
    n8n -- "Bearer key" --> api
    api --> connectors
    metrics --> db
```

## Trust boundaries

| Boundary | What crosses it | How it is authenticated |
|---|---|---|
| Machine client → `/api/v1/*` | JSON over HTTP | `Authorization: Bearer relay_<id>_<secret>`; the secret is stored as a PBKDF2-HMAC-SHA256 hash with a per-key salt, and every route declares the scope it needs. |
| Storefront / helpdesk → `/api/v1/webhooks/*` | Raw request bytes | `X-RelayOps-Signature: t=…,v1=…` over `"{t}.{raw body}"`, inside a per-source tolerance window, with an idempotency key. No API key: the caller is a system, not an account. |
| RelayOps → subscriber | Signed event JSON | The same scheme, signed with that subscription's secret, so the receiver can verify RelayOps the way RelayOps verifies its callers. |
| RelayOps → Slack | Block Kit JSON | The incoming-webhook URL is the credential and lives only in `SLACK_WEBHOOK_URL`; with no URL the payload is recorded as a fixture receipt instead of being invented. |
| Browser → `/api/*` | JSON over HTTP | Unauthenticated, same-origin, exactly as in v1.1. This surface is for the operator UI served by the same process; it is not part of the OpenAPI contract, and a deployment that exposes RelayOps publicly must put its own front door in front of it. **[INFERRED]** — a deliberate boundary, stated rather than hidden. |

## Data flow — an order that arrives as a webhook

```mermaid
sequenceDiagram
    participant S as Storefront
    participant N as n8n
    participant R as RelayOps API
    participant W as Workflow engine
    participant D as SQLite

    S->>N: POST /webhook/relayops-order
    N->>N: map to the canonical order, HMAC the exact bytes
    N->>R: POST /api/v1/webhooks/orders (t=…,v1=…)
    R->>R: verify signature, tolerance, idempotency key
    R->>D: INSERT staged_orders
    R->>W: run workflow 1 (trigger_type=webhook)
    W->>D: canonical order + balanced ledger + KPI cache
    W-->>R: run id, status, row counts
    R->>D: INSERT webhook_receipts
    R-->>N: 202 with the receipt and the run id
    N-->>S: the receipt
```

A replay of the same delivery returns the first receipt, increments its `replays`
counter, and creates nothing. **VERIFIED** by
`tests/test_webhooks_inbound.py::test_replay_returns_the_first_receipt_and_creates_nothing`.

## Data flow — an alert that leaves as a webhook

```mermaid
sequenceDiagram
    participant W as Workflow engine
    participant A as Alert lifecycle
    participant O as Outbox
    participant T as Scheduler tick
    participant N as n8n
    participant C as Slack connector

    W->>A: terminal failure creates an alert
    A->>O: queue alert.created for every matching subscription
    T->>O: pick due rows
    O->>N: signed POST (attempt 1..5, backoff with jitter)
    N-->>O: 200
    O->>A: write the delivery receipt into the alert timeline
    N->>C: POST /api/v1/connectors/slack/notify
    C-->>N: live send, or a labelled fixture receipt
    N->>A: POST /api/v1/alerts/{id}/transition (acknowledged)
```

A receiver that never answers is retried five times with exponential backoff and
jitter, then moved to `dead_letter` — visible on the Webhooks screen with a
**Retry now** button, never silently dropped. **VERIFIED** by
`tests/test_webhooks_outbound.py::test_exhausted_attempts_dead_letter_and_can_be_retried`.

## Why these shapes

- **One route table.** `app/api/__init__.py` is the only description of what the
  server answers, and `tests/test_openapi_contract.py` compares it with
  `docs/openapi.v1.yaml` in both directions. A route without documentation and a
  document without a route both fail the build. See ADR 0002.
- **The outbox lives in the same transaction as the business change.** An alert
  and the intent to tell the world about it are written together, so a crash
  between them is not possible; delivery is a separate, retryable concern.
- **The delivery worker runs inside the existing scheduler thread** rather than in
  a new process, because RelayOps is deliberately one process with no broker. The
  tick already holds a lock, so deliveries cannot overlap. See ADR 0004.
- **The runtime stays standard library only.** Dev tooling may use packages; the
  application may not, and `tests/test_runtime_boundary.py` enforces it. See ADR 0001.

## Where things live

```text
app/api/              route table, scoped API keys, error envelope, handlers
app/errors.py         the shared ApiError/Response types (outside app.api, on purpose)
app/signing.py        HMAC-SHA256 signing and verification, both directions
app/webhooks.py       inbound: verify, map, stage, run, receipt
app/events.py         outbound: subscriptions, outbox, delivery worker, dead-letter
app/connectors/       outward connector adapters (Slack, live or fixture)
app/metrics.py        Prometheus text format from stdlib counters
app/logs.py           one JSON line per request, with its request id
docs/openapi.v1.yaml  the hand-authored contract, served and tested
integrations/n8n/     three workflows exported from a real n8n instance
integrations/receiver/ a signed-webhook receiver for watching deliveries
```
