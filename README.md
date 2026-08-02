# RelayOps — AI Automation & Integration Command Center

![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/data-SQLite-0f80cc?logo=sqlite&logoColor=white)
![AI fallback](https://img.shields.io/badge/AI-offline%20fallback-16a36a)
![Tests](https://img.shields.io/badge/tests-56%20passing-16a36a)
![Release](https://img.shields.io/badge/release-v1.1.0-5b4ce8)

RelayOps is a self-contained retail operations control plane. Version 1.1 executes its workflows against SQLite, measures real runtimes and row counts, dispatches cron jobs from a background scheduler, lets operators build workflows in the UI, and tracks alerts from acknowledgement through resolution.

The application runtime has no third-party Python dependency and requires no key. An optional OpenAI-compatible classification provider can be enabled with an environment variable; a deterministic offline adapter remains the automatic fallback.

![RelayOps executive overview](examples/overview.png)

## What is real in v1.1

- Order automation reads twelve staged POS/web orders, validates them, writes canonical orders, posts balanced debit/credit ledger entries, and refreshes the KPI cache.
- Support automation reads only unclassified tickets, calls `LLMAdapter`, persists category/confidence/priority, applies SLA rules, and writes queue assignments.
- Inventory automation snapshots stock, refreshes the forecast cache, creates deduplicated replenishment tasks, and records buyer-queue messages.
- Finance close aggregates sales rows, reconciles the posted ledger, writes HTML/CSV files, and records distribution.
- Every attempt has a clock-measured duration, actual row count, output/error payload, and configured retry/backoff policy. Terminal failures persist downstream skips and create an alert.
- A daemon scheduler evaluates five-field UTC cron expressions, fires workflow and report jobs, advances next-run times, records scheduler events, and enforces overdue alert escalation.
- The workflow builder creates, edits, reorders, enables, and pauses persisted definitions and executable steps.
- Alerts follow the strict lifecycle `open → acknowledged → investigating → resolved`, with mute rules, delivery receipts, escalation levels, and an immutable timeline.

The six connector surfaces are deterministic local adapters, not claims of access to live vendor tenants. “WhatsApp” and “Email” outputs are durable delivery receipts in the local store. Connector sync controls refresh real SQLite checkpoints. This boundary keeps a fresh clone reproducible while the workflow engine itself performs genuine data work.

## Run it

Requirements: Python 3.9 or newer.

```bash
git clone https://github.com/Mohamed3042/ai-automation-command-center.git
cd ai-automation-command-center
python3 server.py --reset
```

Open [http://127.0.0.1:4173](http://127.0.0.1:4173).

`--reset` recreates the same seeded business snapshot and clears prior execution evidence. Omit it to preserve workflows, runs, lifecycle events, and report metadata. The scheduler runs only while the server process is alive.

Verify the release from another terminal:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_live.py
```

Run the browser audit and recapture all seven application screens plus the generated report preview:

```bash
npm install
node scripts/audit_ui.mjs http://127.0.0.1:4173
npm run screenshots -- http://127.0.0.1:4173
```

Node and Chrome are development-only screenshot tools; neither is part of the server runtime.

## Architecture

```mermaid
flowchart LR
    subgraph Inputs[Seeded adapter boundary]
      POS[POS / web orders]
      MAIL[Support inbox]
      STOCK[Inventory]
    end
    subgraph Runtime[RelayOps process]
      HTTP[Threaded HTTP API]
      SCHED[Background cron scheduler]
      ENGINE[Workflow engine\nsavepoints · retries · clocks]
      ACTIONS[Executable action registry]
      LLM[LLMAdapter\nhosted optional · offline fallback]
      ALERTS[Alert lifecycle + escalation]
      REPORTS[HTML / CSV renderer]
    end
    DB[(SQLite + WAL)]
    UI[Operator command center]
    FILES[Report vault]

    POS & MAIL & STOCK --> DB
    UI <--> HTTP <--> DB
    SCHED --> ENGINE --> ACTIONS --> DB
    ACTIONS --> LLM
    SCHED --> ALERTS --> DB
    SCHED --> REPORTS --> FILES
```

SQLite savepoints wrap each workflow attempt. A failed action is rolled back before its attempt record is written; successful attempts commit their business mutation and evidence together. The scheduler uses its own connection and a tick lock, so HTTP-triggered checks and the background thread cannot dispatch the same in-process tick concurrently.

## Executable action catalog

| Action | Concrete store effect |
|---|---|
| `webhook.validate` | Validates pending staged-order JSON and marks valid rows |
| `transform.map` | Inserts canonical orders and advances staging state |
| `accounting.post` | Writes balanced receivable/sales ledger pairs |
| `metrics.increment` | Upserts ingested-order, revenue, and ledger-variance KPIs |
| `message.receive` | Reads unclassified ticket IDs |
| `ai.classify` | Classifies and updates tickets through `LLMAdapter` |
| `rules.evaluate` | Applies support SLA policy or creates replenishment tasks |
| `ticket.assign` | Persists functional queue assignments |
| `sheet.read` | Writes current inventory snapshots |
| `ai.forecast` | Computes and caches the 14-day projection |
| `message.send` | Writes deduplicated buyer-queue delivery receipts |
| `sales.aggregate` | Upserts daily close totals from sales rows |
| `accounting.reconcile` | Reads the posted ledger and enforces variance tolerance |
| `report.render` | Generates and indexes report files |
| `email.send` | Records a deduplicated finance distribution receipt |

## Retry and failure injection

Each step owns `retry_limit` and `retry_backoff_ms`; total attempts are `1 + retry_limit`. Tests and the operator retry drill use a targeted `FailureInjector`:

```json
{
  "failure_injection": {
    "action": "accounting.reconcile",
    "fail_attempts": 1,
    "message": "Transient adapter fault"
  }
}
```

This fails only the named action for the requested number of attempts. It is explicit, bounded, and injectable; ordinary business failures still originate in action validation and reconciliation logic.

## Scheduler semantics

- Five fields: minute, hour, day of month, month, weekday.
- Supports wildcards, steps such as `*/30`, comma lists, and numeric ranges.
- Uses UTC; the UI shows both the cron expression and localized next-run time.
- Standard cron day-of-month/day-of-week OR behavior is implemented when both are restricted.
- Missed schedules fire once on the next tick and advance from the current time rather than replaying an unbounded backlog.
- Workflow runs, report runs, errors, scheduled-for time, and fired-at time are persisted in `scheduler_events`.
- Overdue unresolved alerts are delivered at the next escalation level unless an active source/severity mute applies.

## AI provider configuration

Offline mode is the default:

```bash
python3 server.py --reset
```

To use a hosted OpenAI-compatible chat-completions endpoint for support classification:

```bash
export RELAYOPS_LLM_API_KEY="..."
export RELAYOPS_LLM_MODEL="gpt-4.1-mini"                    # optional
export RELAYOPS_LLM_ENDPOINT="https://api.openai.com/v1/chat/completions"  # optional
python3 server.py
```

Provider configuration, active mode, model, last use, and fallback errors are visible in AI Intelligence and `/api/health`. The API key is never returned or stored. A provider error falls back to deterministic keyword rules for that classification.

## Hours-saved calculation

RelayOps does not store a headline hours constant. It calculates cumulative time returned from successful attempt evidence:

`Σ(manual_minutes × actual successful records)` for per-record steps, plus `manual_minutes` once for each successful per-run step.

The estimates are deliberately visible and editable in the builder:

| Workflow step | Estimate | Basis |
|---|---:|---|
| Validate staged orders | 0.10 min | per record |
| Normalize orders | 0.35 min | per record |
| Post ledger entries | 0.60 min | per order |
| Refresh KPI cache | 4.00 min | per run |
| Read unclassified conversations | 0.25 min | per record |
| Classify intent | 1.20 min | per record |
| Score priority | 0.40 min | per record |
| Route to queue | 0.60 min | per record |
| Snapshot stock levels | 0.35 min | per record |
| Refresh demand forecast | 6.00 min | per run |
| Create replenishment tasks | 1.20 min | per task |
| Notify buyer queue | 0.50 min | per message |
| Aggregate channel totals | 12.00 min | per run |
| Reconcile ledger | 15.00 min | per run |
| Generate daily close report | 8.00 min | per run |
| Record report distribution | 2.00 min | per run |

After the release verifier’s observed run set, the UI reports 161.9 minutes (2.7 hours). A fresh reset begins at zero execution-derived minutes before any due scheduler job fires.

## Seeded analytical results

The fixed snapshot contains 1,764 store/category daily aggregates, six stores/channels, ten SKUs, twelve support conversations, twelve staged orders, four seeded workflows, and six connector contracts.

| Measure | Deterministic snapshot result |
|---|---:|
| Seven-day net revenue | $1,860,351 |
| Seven-day orders | 27,341 |
| Gross margin | 36.5% |
| High-confidence sales anomalies | 2 |
| Fourteen-day revenue forecast | $4.085M at 91% confidence |
| Support auto-routing after classification | 83.3% |
| Order verifier | 12 canonical orders, 24 balanced ledger rows |
| Terminal retry exercise | 3 attempts, 2 retries, downstream skips, alert created |

Workflow volume, success rate, duration, row counts, scheduler success, and hours returned intentionally start from observed execution evidence instead of seeded claims.

## Screens

| Integrations | Automation operations |
|---|---|
| ![Integration map](examples/integrations.png) | ![Truthful workflow runs](examples/automations.png) |

| Workflow builder | AI intelligence |
|---|---|
| ![Workflow builder](examples/builder.png) | ![AI provider and intelligence](examples/intelligence.png) |

| Reports | Alert lifecycle |
|---|---|
| ![Scheduler and report vault](examples/reports.png) | ![Alert lifecycle timeline](examples/alerts.png) |

## API surface

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Database, scheduler, version, and provider state |
| `GET` | `/api/dashboard` | KPIs, timeline, drill-downs, audit activity |
| `GET/POST` | `/api/connectors`, `/api/connectors/{id}/sync` | Connector state and checkpoint refresh |
| `GET/POST` | `/api/workflows`, `/api/workflows` | List and create definitions |
| `PUT` | `/api/workflows/{id}` | Edit metadata and ordered steps |
| `POST` | `/api/workflows/{id}/toggle` | Enable or pause event/scheduled execution |
| `POST` | `/api/workflows/{id}/run` | Execute with optional targeted failure injection |
| `GET` | `/api/scheduler` | Scheduler state, jobs, next runs, and events |
| `POST` | `/api/scheduler/tick` | Operator/test tick; optional ISO `now` |
| `GET/POST` | `/api/reports`, `/api/reports/generate` | Schedules, artifacts, and manual generation |
| `GET` | `/api/reports/download/{file}` | Download an indexed artifact |
| `GET/POST` | `/api/alerts`, `/api/alerts/evaluate` | Lifecycle queue and rule evaluation |
| `POST` | `/api/alerts/{id}/transition` | Advance exactly one lifecycle state |
| `POST` | `/api/alerts/mutes` | Create a timed source/severity mute |
| `POST` | `/api/alerts/mutes/{id}/toggle` | Enable or disable a mute |
| `GET/POST` | `/api/intelligence`, `/api/intelligence/refresh` | Provider status and deterministic intelligence |

## Upgrade behavior

Opening a v1.0 database applies additive migrations. Existing records are preserved. Known v1.0 seeded workflow runs are marked `legacy_seed` and excluded from observed success/time metrics, while the four seeded step definitions receive their v1.1 action, retry, and estimate metadata. A fresh `--reset` contains no fabricated workflow-run history.

## Repository map

```text
app/actions.py       executable SQLite workflow actions
app/alerts.py        lifecycle, mutes, delivery, escalation
app/db.py            schema, v1 migration, deterministic seed
app/engine.py        savepoint execution, retries, builder persistence
app/intelligence.py  hosted provider boundary + offline intelligence
app/reports.py       HTML/CSV renderer and observed schedule metrics
app/scheduler.py     cron parser, background dispatcher, next-run payload
static/              seven-screen no-framework command center
tests/               56 unit, persistence, scheduler, provider, and HTTP tests
scripts/             live verifier, browser audit, screenshot capture
examples/            live UI captures and generated report examples
server.py            standard-library HTTP entry point
```

Release evidence and the three detailed running-app passes are recorded in [QUALITY_LOG.md](QUALITY_LOG.md) and [RELEASE_NOTES.md](RELEASE_NOTES.md).

## License

MIT
