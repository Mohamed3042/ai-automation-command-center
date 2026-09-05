# n8n workflows

Three workflows that make RelayOps part of a wider automation estate. They are
**exports from a real n8n instance**, not hand-written files: each one was drafted,
imported into the `n8nio/n8n:2.37.10` container in [`compose.yaml`](../../compose.yaml),
executed there until it passed, and exported back with
`n8n export:workflow --all --separate --pretty`. The CI job `n8n-e2e` re-imports
them into a fresh container on every push and fires a real order through them, so
a workflow that stops working fails the build.

| File | Workflow | Trigger | What it does |
|---|---|---|---|
| `01-order-intake.json` | RelayOps - order intake | Webhook `POST /webhook/relayops-order` | Maps a storefront order onto the RelayOps canonical shape, signs it with HMAC-SHA256 in a Code node, posts it to `/api/v1/webhooks/orders`, and returns the RelayOps receipt to the caller. |
| `02-alert-escalation.json` | RelayOps - alert escalation | Webhook `POST /webhook/relayops-alert` | Receives a RelayOps outbound alert event, and when the severity is `high` or `critical` posts the Block Kit message through `/api/v1/connectors/slack/notify` and calls back into RelayOps to acknowledge the alert. |
| `03-nightly-report.json` | RelayOps - nightly report | Cron `0 2 * * *`, plus an on-demand trigger | Generates the daily report, downloads the HTML artifact, and writes it to the mounted `/reports` folder. No mail credentials are involved. |

## Import in three commands

With the stack running (`docker compose up -d`):

```bash
docker compose exec n8n n8n import:workflow --separate --input=/workflows
docker compose exec n8n n8n update:workflow --id=relayops-order-intake --active=true
docker compose exec n8n n8n update:workflow --id=relayops-alert-escalation --active=true
```

Then `docker compose restart n8n` — n8n registers production webhook routes at
start-up, so an activation only takes effect after a restart. (The CLI says so
itself: *"Changes will not take effect if n8n is running."*)

Send a test order:

```bash
curl -sS -X POST http://localhost:5678/webhook/relayops-order -H 'Content-Type: application/json' -d '{"id":"N8N-70001","created_at":"2026-08-02T11:05:00Z","currency":"USD","total_price":"321.75","financial_status":"paid","location":{"name":"Harbour East"}}'
```

The response is the RelayOps receipt, including the id of the automation run that
processed the order. Run the nightly report without waiting for 02:00:

```bash
docker compose exec n8n n8n execute --id=relayops-nightly-report
docker compose exec n8n ls -la /reports
```

## Configuration

The workflows read everything from the environment, so no credential is stored in
the JSON. `compose.yaml` sets them on the n8n container:

| Variable | Used by | Meaning |
|---|---|---|
| `RELAYOPS_BASE_URL` | all three | Where RelayOps is reachable from n8n (`http://relayops:4173` inside Compose). |
| `RELAYOPS_WEBHOOK_SECRET` | order intake | The secret the Code node signs with. It must equal the RelayOps `orders` source secret. |
| `RELAYOPS_API_KEY` | escalation, nightly report | A RelayOps API key, `relay_<id>_<secret>`. Compose passes `RELAYOPS_BOOTSTRAP_API_KEY` through. |
| `RELAYOPS_REPORT_DIR` | nightly report | Where the downloaded artifact is written (`/reports`). |
| `NODE_FUNCTION_ALLOW_BUILTIN=crypto` | order intake | Lets the Code node `require('crypto')` to compute the HMAC. |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | all three | Lets expressions read `$env`. |
| `N8N_RESTRICT_FILE_ACCESS_TO=/reports` | nightly report | The only path the workflows may write. |

## Notes paid for while building these

- **Importing overwrites the active flag.** `import:workflow` applies the `active`
  value in the file, so activation belongs *after* the import, never before.
- **A named volume is created root-owned.** n8n runs as uid 1000 and cannot write
  into a fresh `/reports`; the `n8n-reports-owner` init service in `compose.yaml`
  sets the ownership once, before n8n starts.
- **n8n refuses to write inside its own data directory** (`N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES`
  defaults to true), so the report drop cannot live under `~/.n8n`.
- **`n8n execute` needs a callable trigger.** The nightly workflow carries a
  *Run on demand* trigger beside its cron trigger so CI and operators can run it
  without waiting for 02:00.
- **The signature covers the exact bytes.** The Code node builds the JSON string
  and the HTTP node sends that same string as a raw body; re-serialising the
  object in the HTTP node would produce a different byte order and a 401.
