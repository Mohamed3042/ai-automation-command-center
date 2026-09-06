# RelayOps — v1.2 gives the control plane a signed integration edge

**What:** RelayOps can now be driven and observed from outside its own process — a
versioned `/api/v1` with scoped API keys and a hand-authored OpenAPI 3.1 contract,
HMAC-SHA256 webhooks in both directions with idempotent receipts and a retrying
outbox, three real n8n workflows, a Slack connector, a Docker image, Prometheus
metrics and JSON logs — while the application runtime is still standard-library
only, and a test now enforces that instead of the README promising it.

**Proof:** `python3 -m unittest discover -s tests` → `Ran 121 tests` / `OK`, measured
on Python 3.9.25 and 3.14.6 (up from 56 tests). Every new gate was shown RED on a
sabotaged copy before it was shown green — including the n8n end-to-end check, which
was given the wrong signing secret and answered `signature_invalid` with exit 1.
Live, against `docker compose up`: an order posted at n8n's production webhook
arrived signed and became RelayOps run `success` with 6 row results; a forced
workflow failure raised alert 6, whose `alert.created` event was delivered to n8n
(HTTP 200, attempt 1), came back as a Slack **fixture** receipt, and was acknowledged
by n8n — both delivery receipts visible in that alert's immutable timeline. The
container reaches `healthy` as uid 10001, answers `401` without a key and `200` with
one. CI run 34002865529 is green on all five jobs including `n8n-e2e` (1m 48s):
<https://github.com/Mohamed3042/ai-automation-command-center/actions/runs/34002865529>

**Boundary:** there is no Slack workspace attached to this repository, so with no
`SLACK_WEBHOOK_URL` the Block Kit message is built and stored as a receipt the UI
labels **fixture** — the message is real, the transport is not, and the interface says
which. The business data stays synthetic and every page says so. The browser UI keeps
using the unauthenticated same-origin `/api/*` surface from v1.1, which is
deliberately not part of the OpenAPI contract.

**Shots:** 01-webhooks-screen.png — the new Webhooks screen on a live server carrying
real traffic: four inbound receipts with one replay refused, an outbound outbox where
eight events delivered and one dead-lettered after five attempts with a retry control,
and the Slack connector honestly labelled *fixture*.
**Shots:** 02-api-reference.png — `/api/v1/docs`, the vendored Redoc page rendering the
hand-authored OpenAPI 3.1 document that a test compares with the router in both
directions.
**Shots:** 03-ci-n8n-e2e-green.png — the CI run: five jobs green on a clean Ubuntu
runner, with `n8n-e2e` importing the three workflows into a fresh n8n container,
firing a real order through them, and asserting the nightly report artifact was
written.

**LinkedIn paste:** RelayOps v1.2 turns a self-contained retail operations control
plane into something other systems can drive: a versioned REST API with scoped keys,
HMAC-SHA256 webhooks in both directions with idempotent receipts and a retrying
outbox that dead-letters instead of losing events, and three n8n workflows that CI
imports into a real n8n container and executes on every push. The whole runtime is
still Python standard library only — and now a test fails the build if that ever stops
being true. Every gate in this release was shown failing on a deliberately broken copy
before it was shown passing.

**Surfaces:** [ ] showcase-pdf [ ] resume [ ] website [ ] linkedin
