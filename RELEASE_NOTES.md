# RelayOps v1.0.0

First production-quality portfolio release of the AI Automation & Integration Command Center.

## Highlights

- Six visible retail connector contracts: Square POS, Shopify Plus, Oracle NetSuite, Microsoft 365 Mail, WhatsApp Business, and Google Sheets.
- Live health/latency/throughput integration map with manual sync controls.
- Four-workflow automation engine with chained steps, persisted runs, retries, failure branches, and escalation.
- Deterministic, zero-key AI adapter powering sales anomaly detection, demand forecasting, inventory risk, and support auto-triage.
- Drillable KPI dashboard covering $1.86M seven-day net revenue and 27,341 orders in the fixed demo snapshot.
- Daily, weekly, and inventory report schedules with real HTML/CSV outputs and a downloadable report vault.
- Severity-based alert engine with acknowledgement windows, deduplication, escalation policy, and 24 visible delivery receipts after the release exercise.
- Six major-screen screenshots plus a generated-report preview captured from the running application.

## Verification

- `python3 -m unittest discover -s tests -v` — 7 tests passed.
- `python3 scripts/verify_live.py` — live HTTP verification passed.
- Successful order workflow — 168 records, four completed steps, zero retries.
- Forced finance-close failure — three retries, failed step persisted, downstream step skipped, alert escalated.
- Alert evaluation — seven rules matched, seven new alerts, thirteen delivery receipts recorded.
- Daily and weekly reports — HTML and CSV artifacts generated and inspected.
- `npm audit --audit-level=high` — zero vulnerabilities.

## Runtime

Python 3.9+ and SQLite. No third-party Python dependency and no external AI key required.
