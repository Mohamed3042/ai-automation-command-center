# RelayOps v1.1 quality iteration log

Release-candidate work was performed against reset SQLite databases and the running server on 02 August 2026. These were separate passes, not a retrospective description of one screenshot run.

## Pass 1 — execution truth and first live visual inspection

- Compiled all Python modules and ran the initial seven-test suite to identify v1 assertions tied to fabricated history and `force_error`.
- Exercised all four workflows directly against a temporary SQLite store.
- Verified twelve staged orders became twelve canonical orders and twenty-four balanced ledger entries; four support cases were classified/routed; ten replenishment tasks and buyer receipts were written; finance close reconciled the ledger and wrote report files.
- Injected a one-attempt transient fault and observed a real retry succeed. Injected a terminal fault and observed configured attempts, downstream skips, and alert creation.
- Started the reset server, passed the expanded live verifier, captured seven screens, and inspected Automations, Builder, Intelligence, Reports, and Alerts at release resolution.
- Findings fixed:
  - replaced the reports page’s constant 100% delivery claim with observed report-scheduler outcomes;
  - refreshed global alert/provider status on every view so sidebar state could not go stale;
  - clarified that the report vault includes scheduler, workflow, and operator output;
  - exposed editable manual-effort assumptions beside each executable step.

## Pass 2 — browser interaction and state-transition audit

- Added a reusable Puppeteer audit that listens for console errors, page errors, failed requests, loading failures, and horizontal overflow across all seven views.
- Through the live form, created a workflow, added a step, reordered the steps, and saved it.
- Triggered the operator retry drill and verified its HTTP execution response and completion toast.
- Advanced an alert lifecycle state and created a mute through the UI.
- Findings fixed:
  - added an explicit wait for the alerts rerender before interacting with the mute form; this removed a real browser timing race;
  - made connector checkpoint refreshes write truthful audit events and return the persisted source counter instead of an arithmetic value;
  - added health-check database probing and JSON-object validation;
  - marked v1.0 seeded runs as legacy evidence during migration and excluded them from observed metrics.

## Pass 3 — release-candidate provenance and resilience audit

- Reset again, reran the live verifier, reran the interactive browser audit, and recaptured the entire UI.
- Inspected all seven screen captures plus the generated weekly report image for clipping, overflow, stale navigation state, inconsistent lifecycle controls, source labeling, and next-run visibility.
- The browser’s rapid page close exposed a harmless but noisy server `BrokenPipeError`; response writes now tolerate client disconnects without attempting a second error response.
- The generated weekly file revealed a provenance mismatch: an operator-generated file called itself scheduler-generated. Added `generated_by` report metadata and propagated the actual source (`Ops operator`, `Background scheduler`, or workflow run) into the database, report library, audit event, and rendered HTML.
- Performed one final clean reset and verifier run, then captured all release images and copied newly generated daily/weekly HTML/CSV examples.
- Ran a read-only seven-view browser audit after capture; no console errors, request failures, load failures, or horizontal overflow were detected.

## Final command evidence

| Check | Result |
|---|---|
| `python3 -m py_compile server.py app/*.py scripts/verify_live.py` | Passed |
| `python3 -m unittest discover -s tests -v` | 56 tests passed |
| `python3 scripts/verify_live.py` | Passed against reset live server |
| `node scripts/audit_ui.mjs …` | Passed seven views plus builder/retry/lifecycle/mute interactions |
| `node scripts/audit_ui.mjs … --read-only` | Passed final seven-view release audit |
| `npm run screenshots -- …` | Captured seven screens and generated report preview |
| `npm audit --audit-level=high` | 0 vulnerabilities |
| `git diff --check` | Passed |

## Live verifier evidence

- Order pipeline: 39 truthful action row results, including 12 canonical orders and 24 ledger writes.
- Support pipeline: 16 action row results and 83.3% high-confidence routing across the inbox.
- Retry evidence: one transient retry recovered; terminal injection made three attempts/two retries and persisted downstream skips.
- Builder: workflow created, reordered, paused, enabled, and executed.
- Scheduler: four due jobs fired in the deterministic future-time drill; five next-run timestamps surfaced.
- Alert: workflow failure acknowledged, investigated, and resolved with at least five timeline entries; mute persisted.
- Time returned: 161.9 minutes derived from the verifier’s successful step volumes and documented estimates.
- Report vault: 12 artifacts after scheduled, workflow, and manual generation; two observed report-scheduler runs at 100% success.

## Release artifacts inspected

- `examples/overview.png`
- `examples/integrations.png`
- `examples/automations.png`
- `examples/builder.png`
- `examples/intelligence.png`
- `examples/reports.png`
- `examples/alerts.png`
- `examples/generated-weekly-report.png`
- `examples/reports/daily-trading-brief.html` and `.csv`
- `examples/reports/weekly-operations-review.html` and `.csv`
