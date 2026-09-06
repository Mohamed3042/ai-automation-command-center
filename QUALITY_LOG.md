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

## v1.2 — the integration edge (05 September 2026)

Work was performed against reset SQLite databases, a live server, a built container,
and a real `n8nio/n8n:2.37.10` instance. Each pass below is a separate run.

### Pass 1 — the API layer, against the v1.1 promises

- Refactored the router out of `server.py` into `app/api/` while leaving every v1.1
  route answering exactly as before; the 56 existing tests passed unchanged.
- Two defects found and fixed by their own tests before anything else was built:
  - API tokens were generated with `secrets.token_urlsafe`, whose alphabet contains
    `_`, so `relay_<id>_<secret>` split into four parts and every key was rejected.
    Tokens are now hex, and `parse_token` splits with `maxsplit=2` so a bootstrap
    token with underscores still parses.
  - An unmatched `/api/v1/...` path fell through to the v1.1 handler and answered in
    the old error shape. `handles()` now claims the whole prefix, so the versioned
    API owns its own 404s.
- Split `ApiError`/`Response` into `app/errors.py`: `app.webhooks` and `app.events`
  are imported *by* the API package, so importing from it was a circular import.

### Pass 2 — the contract, and a reader for it

- Wrote `docs/openapi.v1.yaml` by hand and `tests/support/yamlmini.py`, a
  standard-library reader for the exact YAML subset it uses, so the coverage gate
  runs in the dependency-free CI job.
- The reader is not trusted: its parse is compared with PyYAML on the real document,
  and the document is validated against the OpenAPI 3.1 schema by
  `openapi-spec-validator`. Both were measured equal/valid, and the CI job fails if
  either check is skipped instead of run.
- Sabotage confirmed the gate sees both directions of drift.

### Pass 3 — signatures, idempotency and the outbox

- Inbound: verified a tampered body, a stale timestamp, a wrong secret, a missing
  header and a malformed header each produce a distinct code, and that a rejected
  delivery stages nothing.
- Idempotency: a replay returns the first receipt, increments `replays`, and creates
  no second staged order.
- Outbound: a receiver fixture scripted to answer 500, 500, 200 produced three
  recorded attempts and one delivery receipt in the alert timeline; five failures
  produced a `dead_letter` that refuses to retry itself and is requeued only by the
  operator control.
- A connection error is stored with no status code and its reason, rather than a
  fabricated zero.

### Pass 4 — n8n, on a real instance

- Three workflows drafted, imported into the running container, executed until they
  passed, and exported back. Four traps were paid for and are written into
  `integrations/n8n/README.md`:
  - `import:workflow` applies the file's `active` flag, so importing deactivates a
    running workflow unless activation comes afterwards;
  - a named Docker volume is created root-owned and n8n runs as uid 1000, so the
    report drop needed an ownership init service;
  - n8n refuses to write inside its own data directory, so the drop cannot live
    under `~/.n8n`;
  - `n8n execute` needs a callable trigger, so the nightly workflow carries one
    beside its cron trigger.
- The end-to-end check was proved sighted by giving the n8n container the wrong
  signing secret: RelayOps answered `signature_invalid` and the script exited 1.

### Pass 5 — container, screens and the live pass

- Built the multi-stage image, confirmed the health check reaches `healthy`, the
  process runs as uid 10001, an unauthenticated call is `401` and a keyed call is
  `200`.
- Recaptured all eight screens and the API reference from a live server carrying real
  webhook traffic: four inbound receipts (one replay refused), nine outbound events
  (eight delivered, one dead-lettered), two subscriptions, and a Slack fixture
  receipt.
- Generalised `scripts/capture_screenshots.mjs` to find Chrome on macOS, Windows and
  Linux instead of a hardcoded macOS path.
- Ran the whole suite on Python 3.9.25 as well as 3.14.6 to keep the README's
  "Python 3.9+" claim measured rather than assumed.
