# RelayOps v1.2.0

v1.1 made RelayOps execute real work. v1.2 lets that work be driven and observed from
outside the process — a versioned REST API, signed webhooks in both directions, three
n8n workflows, a Slack connector, a Docker image, metrics and structured logs — while
the application runtime stays standard library only.

## Highlights

- **`/api/v1` with scoped API keys.** A single route table in `app/api/` serves 33
  documented paths behind one error envelope (`code`, `message`, `request_id`), with
  `limit`/`offset` pagination on every list route and an `X-Request-Id` on every
  response. Keys are minted with `python3 server.py keys create --name <name>
  --scopes <scopes>`, stored as a PBKDF2-HMAC-SHA256 hash with a per-key salt, and
  every route declares the scope it requires.
- **A hand-authored OpenAPI 3.1 document** at `docs/openapi.v1.yaml`, served at
  `/api/v1/openapi.yaml` and rendered by a vendored Redoc page at `/api/v1/docs`.
  A test compares it with the router in both directions, so a route without a
  specification entry and an entry without a route both fail the build.
- **Inbound signed webhooks.** `POST /api/v1/webhooks/orders` and `/tickets` verify
  `X-RelayOps-Signature: t=…,v1=…` (HMAC-SHA256 over `"{t}.{raw body}"`) inside a
  300-second tolerance window, map the payload onto the rows the existing automations
  consume, run the matching workflow, and answer `202` with a receipt naming the run.
  A replay returns the first receipt and creates nothing.
- **Outbound signed webhooks.** Subscriptions with their own secrets and event
  filters, a transactional outbox written in the same transaction as the business
  change, and a delivery worker inside the existing scheduler tick: five attempts,
  exponential backoff with up to 20 % jitter capped at 300 s, then `dead_letter` with
  an operator retry. Alert-linked deliveries write their outcome into that alert's
  immutable timeline.
- **Three n8n workflows** in `integrations/n8n/`, exported from a running
  `n8nio/n8n:2.37.10`: order intake (webhook → HMAC in a Code node → RelayOps),
  alert escalation (RelayOps event → severity filter → Slack Block Kit → acknowledge
  back in RelayOps), and a nightly report drop (cron → generate → download → mounted
  folder). CI re-imports and executes them on every push.
- **A Slack connector** (`app/connectors/slack.py`) posting Block Kit messages to an
  incoming webhook, or — with no `SLACK_WEBHOOK_URL` — recording the identical
  payload as a receipt the UI labels **fixture**.
- **A Webhooks screen**: inbound receipts (click a row to see what it created),
  the outbox with attempts and a retry button, subscriptions with an add form, the
  inbound endpoints with their tolerance and where each secret comes from, and the
  Slack connector's mode.
- **Docker.** Multi-stage `python:3.12.7-slim`, non-root uid 10001, `HEALTHCHECK` on
  `/api/v1/ready`; `compose.yaml` runs RelayOps beside n8n, with a `receiver` profile
  for watching deliveries. CI publishes to
  `ghcr.io/mohamed3042/ai-automation-command-center`.
- **Observability.** `GET /metrics` in Prometheus text format from standard-library
  counters, and one JSON log line per request carrying the request id the caller
  received.
- **Docs.** `docs/architecture.md` (C4 context and containers, trust boundaries, two
  sequence flows), five ADRs, `docs/webhooks.md` with a fifteen-line verifier, and
  `docs/demo-runbook.md`.

## Verification evidence

Every new gate was shown RED on a sabotaged copy before it was shown green.

| Gate | Sabotage | RED line |
|---|---|---|
| standard-library runtime | `import yaml` added to `app/metrics.py` | `AssertionError: {'app/metrics.py': ['yaml']} != {}` |
| OpenAPI covers every route | an extra route in the table | `Lists differ: ['/api/v1/undocumented'] != []` |
| every documented path is served | an extra path in the document | `Lists differ: ['/api/v1/ghost'] != []` |
| inbound signature verification | `verify(...)` replaced with `pass` | `AssertionError: 202 != 401` on a tampered body |
| inbound idempotency | the replay lookup forced to `None` | `Tuples differ: (202, 409) != (202, 202)` |
| outbound dead-letter | the attempt ceiling disabled | `Tuples differ: ('retrying', 5) != ('dead_letter', 5)` |
| API-key scope enforcement | an early `return` in `require_scope` | `AssertionError: 200 != 403` |
| the n8n end-to-end check | the wrong signing secret on the n8n container | `signature_invalid`, script exit 1 |

- `python3 -m unittest discover -s tests` — **121 tests, OK**, measured on Python
  3.9.25 and 3.14.6. The 3.9 run confirms the README's "Python 3.9+" claim.
- `python3 scripts/verify_live.py` — passed against a reset, running v1.2 server.
- Live container — `docker build` then `docker run`: `HEALTHCHECK` reached `healthy`,
  the process runs as uid 10001, `/api/v1/workflows` answered `401` without a key and
  `200` with one, `/metrics` served the Prometheus families.
- Live n8n end to end — `scripts/n8n_e2e.py` against `docker compose up`: an order
  posted at n8n's production webhook arrived at RelayOps with a valid signature and
  became run `success`; a forced workflow failure raised an alert whose
  `alert.created` event was delivered to n8n (HTTP 200, attempt 1), came back as a
  Slack **fixture** receipt, and was acknowledged by n8n — with both delivery receipts
  visible in the alert timeline.
- Live nightly report — `n8n execute --id=relayops-nightly-report` wrote
  `daily-2026-08-02-….html` into the mounted `/reports` folder.
- Local delivery drill — the receiver fixture answering 500, 500, 200 verified the
  RelayOps signature on each attempt and accepted on the third; a subscription
  pointed at an unreachable endpoint reached `dead_letter` after five attempts and
  was requeued by the retry control.

## Runtime and compatibility

- Python 3.9+ standard library and SQLite; still no required Python package.
  `requirements-dev.txt` (PyYAML, openapi-spec-validator) is test tooling only, and
  `tests/test_runtime_boundary.py` fails the build if the runtime ever imports a
  package.
- Existing behaviour is unchanged: the scheduler, alert lifecycle, reports, LLM
  adapter, connectors, the seeded snapshot and `--reset` all behave as in v1.1, and
  the v1.1 tests still pass untouched except for one assertion that now reads the
  version from `app.__version__`.
- The browser UI keeps using the unauthenticated same-origin `/api/*` surface; it is
  not part of the OpenAPI contract.
- Opening a v1.1 database adds the integration tables and seeds the two webhook
  sources; no row is rewritten.
- The seeded webhook signing secrets are development values, published on purpose so
  a fresh clone can sign its own request. `.env.example` documents the environment
  variables that replace them.

## UI evidence

`examples/` was recaptured from a live v1.2 server: the eight application screens
including the new **Webhooks & delivery**, the `/api/v1/docs` reference rendered from
the vendored Redoc bundle, and the generated weekly report.

---

# RelayOps v1.1.0

RelayOps v1.1 replaces the v1.0 simulated automation evidence with an executable, scheduler-driven operations core while preserving the zero-dependency, zero-key local contract.

## Highlights

- Fifteen concrete workflow actions now read and write the SQLite operational store: staged-order ingestion, normalization, ledger posting/reconciliation, KPI caching, support classification/routing, inventory snapshots, replenishment tasks/messages, sales aggregation, and report generation/distribution.
- Durations come from monotonic clocks; record counts come from action results; every attempt is persisted.
- Configurable per-step retry limits and bounded backoff replace the hardcoded retry claim. Targeted `FailureInjector` drills cover transient recovery and terminal exhaustion.
- A daemon scheduler fires UTC cron workflows and report schedules, persists scheduled/fired timestamps and outcomes, advances next-run times, and enforces overdue alert escalation.
- A new Workflow Builder screen creates, edits, reorders, enables, and pauses workflows and steps in SQLite.
- Alerts now move through `open → acknowledged → investigating → resolved`, with source/severity mute rules and a visible escalation timeline.
- `LLMAdapter` supports an optional OpenAI-compatible hosted classification endpoint through `RELAYOPS_LLM_API_KEY`, with automatic offline fallback and visible provider status.
- Hours returned are computed from actual successful record volume and editable, documented per-step manual-minute estimates. The literal 286-hour claim is gone.
- V1.0 seed runs are preserved during migration but marked as legacy and excluded from observed v1.1 execution metrics.

## Verification evidence

- `python3 -m unittest discover -s tests -v` — 56 tests passed on the release candidate.
- `python3 scripts/verify_live.py` — passed against a reset, running server.
- Live order run — 12 staged orders, 12 canonical orders, 24 balanced ledger writes.
- Live support run — four unclassified tickets classified, prioritized, and routed; 83.3% high-confidence routing across the inbox.
- Retry drill — one transient failure recovered on retry; terminal injection made three attempts, recorded two retries and downstream skips, and created an alert.
- Builder drill — workflow created, reordered, paused, enabled, and executed through public APIs; browser audit also created and reordered a definition through the form.
- Scheduler drill — four due jobs fired in the verifier, next runs advanced and surfaced, report artifacts persisted, and due alerts escalated.
- Lifecycle drill — the failure alert was acknowledged, investigated, and resolved; its complete timeline remained queryable.
- Browser audit — seven views checked for runtime errors and overflow, plus builder/retry/lifecycle/mute interactions.
- `npm audit --audit-level=high` — no high-severity vulnerabilities in the release dependency tree.

## Runtime and compatibility

- Python 3.9+ standard library and SQLite; no required Python package installation.
- Deterministic offline AI remains the default and requires no credential or network.
- The scheduler is in-process and runs only while the RelayOps server is running.
- Cron scheduling is UTC. Hosted classification is optional and falls back locally on provider error.
- Additive migration preserves v1.0 data. Use `--reset` only when a clean deterministic demonstration snapshot is wanted.

## UI evidence

All images in `examples/` were recaptured from the v1.1 live server. The set now includes the Workflow Builder along with overview, integrations, automations, intelligence, reports, alerts, and the generated weekly report.

---

## RelayOps v1.0.0

Initial portfolio release with the six-domain command center, deterministic analytical snapshot, report renderer, and first-generation workflow/alert UI. V1.1 retains its working dashboard, connector, analytics, and report surfaces while replacing simulated workflow evidence and schedule labels with executable behavior.
