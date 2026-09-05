# Five-minute demo runbook

What to open, what to click, what the audience sees, and the sentence that keeps it
honest. Everything below has been run; nothing is aspirational.

**Setup (once, before the audience arrives).**

```bash
cp .env.example .env
printf 'RELAYOPS_BOOTSTRAP_API_KEY=relay_demo0001_%s\n' "$(openssl rand -hex 24)" >> .env
docker compose up -d --build
docker compose exec n8n n8n import:workflow --separate --input=/workflows
docker compose exec n8n n8n update:workflow --id=relayops-order-intake --active=true
docker compose exec n8n n8n update:workflow --id=relayops-alert-escalation --active=true
docker compose restart n8n
```

Two tabs: RelayOps at <http://localhost:4173>, n8n at <http://localhost:5678>.
Keep the key handy: `grep RELAYOPS_BOOTSTRAP_API_KEY .env`.

> If port 4173 or 5678 is taken on your machine, set `RELAYOPS_HOST_PORT` /
> `N8N_HOST_PORT` in `.env` and use those instead.

---

## 0:00 — What this is (30 s)

**Say:** "RelayOps is a retail operations control plane. One Python process, SQLite,
no third-party runtime dependency. It runs workflows against real rows, keeps alerts
through a strict lifecycle, and as of v1.2 it talks to the outside world."

**Show:** the Overview screen. Point at *Automation success* — "these numbers come
from executed runs, not from a seed."

**The honest sentence:** "The business data is synthetic and every page says so. The
engine, the API, the signatures and the delivery machinery are real."

## 0:30 — An order arrives from outside (90 s)

**Show:** the n8n tab, workflow *RelayOps - order intake*. Three nodes: a webhook, a
Code node that maps the storefront payload and signs it, an HTTP node that posts it.

**Do:** send an order.

```bash
curl -sS -X POST http://localhost:5678/webhook/relayops-order \
  -H 'Content-Type: application/json' \
  -d '{"id":"DEMO-1001","created_at":"2026-08-02T11:05:00Z","currency":"USD","total_price":"321.75","financial_status":"paid","location":{"name":"Harbour East"}}'
```

**Audience sees:** the response is not "ok" — it is the RelayOps receipt with a run
id and a row count. Switch to RelayOps → **Webhooks**: the receipt is at the top of
*Inbound receipts*. Click it: the idempotency key, the automation it started, and the
SHA-256 of the exact signed bytes.

**Do:** send the same order again. The receipt is the same one, `replays` goes up,
and no second order exists. "Replays are refused by design, not by luck."

**Do:** tamper with it — change `total_price` and reuse the signature. `401
signature_invalid`.

## 2:00 — Something breaks, and the outside world hears about it (90 s)

**Do:** force a terminal failure.

```bash
curl -sS -X POST http://localhost:4173/api/workflows/4/run -H 'Content-Type: application/json' \
  -d '{"failure_injection":{"action":"accounting.reconcile","fail_attempts":10,"message":"Ledger adapter unavailable"}}'
```

**Show:** RelayOps → **Alerts**. A new high-severity alert with a timeline.

**Show:** RelayOps → **Webhooks** → *Outbound deliveries*. Within a second or two,
`alert.created` is `delivered` to `n8n-alert-escalation`.

**Show:** n8n → *RelayOps - alert escalation* → Executions. The IF node let it
through because the severity is high; the next node called back into RelayOps.

**Show:** back in **Webhooks**, the *Slack connector* card. The pill reads `fixture`.

**The honest sentence:** "There is no Slack workspace attached to this repository, so
the Block Kit message is built and recorded as a labelled fixture receipt. With
`SLACK_WEBHOOK_URL` set it is the same payload over a real transport — the interface
tells you which, always."

**Show:** the alert timeline again. It now carries the delivery receipt and the
acknowledgement n8n made. "The feature that already existed — the immutable timeline —
got a real transport."

## 3:30 — Show the contract (45 s)

**Open:** <http://localhost:4173/api/v1/docs> — the vendored Redoc page over the
hand-authored OpenAPI 3.1 document.

**Say:** "This document is served from the repository, and a test compares it with
the router in both directions. A route without documentation fails the build, and so
does a documented path with no route."

**Do:** show that the key is real.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4173/api/v1/workflows          # 401
curl -sS -H "Authorization: Bearer $KEY" 'http://localhost:4173/api/v1/runs?limit=3' | head -c 200
```

## 4:15 — Show it is operable (45 s)

**Do:**

```bash
curl -s http://localhost:4173/metrics | grep -E 'relayops_(alerts|webhook_outbox|http_requests_total)' | head
docker compose logs --tail 3 relayops
```

**Audience sees:** Prometheus families for alerts by state and outbox by status, and
one JSON log line per request carrying the same `request_id` the API returned in its
`X-Request-Id` header.

**Do:** the nightly report, without waiting for 02:00.

```bash
docker compose exec n8n n8n execute --id=relayops-nightly-report
docker compose exec n8n ls -la /reports
```

**Say:** "Cron in n8n, generate and download over the RelayOps API, written to a
mounted folder. No mail credentials in the demo."

## 5:00 — Close

**Say:** "Standard-library runtime, a contract that cannot drift, signatures both
ways, an outbox with dead-letters, and a CI job that re-imports these three n8n
workflows into a fresh container on every push and fires a real order through them.
If that job is green, everything you just saw still works."

**Show:** the CI badge in the README, and the `n8n-e2e` job in the run.

---

## If something goes wrong on stage

| Symptom | Cause | Fix in one line |
|---|---|---|
| n8n webhook returns 404 | the workflow was imported but n8n was not restarted | `docker compose restart n8n` |
| `signature_invalid` from the demo curl | `RELAYOPS_WEBHOOK_SECRET` differs between the two containers | check both with `docker compose exec relayops printenv RELAYOPS_WEBHOOK_SECRET` |
| deliveries stay `retrying` | the subscription URL is not reachable from the RelayOps container | use the service name (`http://n8n:5678/...`), not `localhost` |
| the report workflow says access denied | `/reports` ownership | `docker compose up -d n8n-reports-owner` then restart n8n |
| a port is already in use | something else on the host holds it | set `RELAYOPS_HOST_PORT` / `N8N_HOST_PORT` in `.env` |
