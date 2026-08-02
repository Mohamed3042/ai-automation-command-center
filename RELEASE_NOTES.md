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
