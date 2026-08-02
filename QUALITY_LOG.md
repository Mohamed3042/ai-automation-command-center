# Quality iteration log

The release was exercised against the running server and a fresh SQLite database on 02 August 2026. Each pass included the live UI and generated report output.

## Pass 1 — functional and visual baseline

- Started the real service on `127.0.0.1:4173` with a deterministic database reset.
- Exercised health, dashboard, connector, and AI endpoints.
- Ran a successful order workflow and a forced finance-close failure; confirmed three retries, a skipped downstream step, alert creation, and delivery records.
- Evaluated alert rules and generated daily/weekly HTML and CSV artifacts.
- Captured and inspected all six screens.
- Findings corrected: constrained a workflow metadata SVG, replaced the small demo-run ratio with the weighted 30-day workflow KPI, raised anomaly materiality to suppress noisy deviations, and made navigation counts live.

## Pass 2 — information density and evidence quality

- Reset the database and reran all seven tests plus the public-HTTP verification script.
- Confirmed two material anomaly signals, a 14-day forecast, 83.3% support auto-routing, and seven new rule alerts with thirteen delivery receipts.
- Re-inspected the overview, integration map, workflow catalog/run history, AI inbox, report library, alert queue, escalation policy, and delivery log.
- Opened both generated formats and reconciled the weekly report to 27,341 orders, $1,916,987.65 gross revenue, $700,634.58 gross margin, and six store/channel rows.
- Findings corrected: replaced intermittent one-shot Chrome capture with a controlled Puppeteer session; added semantic styles for urgent/normal/resolved support states.

## Pass 3 — release candidate audit

- Rebuilt the database and report directory from scratch.
- Reran all tests, live success/failure workflows, alert evaluation, daily/weekly generation, and the package vulnerability audit.
- Improved generated-report formatting so currency carries symbols and orders render as integers.
- Captured all six major screens and the real weekly report at release resolution using a document-height viewport, then inspected every final image for clipping, overflow, missing data, and inconsistent states.
- Final result: seven tests passed; live verification passed; zero high-severity package vulnerabilities; UI and report artifacts clean.

## Release artifacts inspected

- `examples/overview.png`
- `examples/integrations.png`
- `examples/automations.png`
- `examples/intelligence.png`
- `examples/reports.png`
- `examples/alerts.png`
- `examples/generated-weekly-report.png`
- `examples/reports/daily-trading-brief.html` and `.csv`
- `examples/reports/weekly-operations-review.html` and `.csv`
