# 0005 - A connector without credentials records a labelled fixture, and says so

Date: 2026-09-05 - Status: accepted

## Context

The Slack connector is the first RelayOps surface that can reach a real third-party
system. No Slack workspace is attached to this repository, and a demo must still show
something.

Three bad options were available: pretend a message was sent; hide the feature behind
a flag so it is invisible; or make the demo depend on a credential the reader does not
have.

## Decision

`app/connectors/slack.py` builds the Block Kit message either way.

- With `SLACK_WEBHOOK_URL` set, it POSTs to the Slack incoming webhook and stores the
  HTTP outcome.
- Without it, the **identical payload** is stored in `connector_messages` with
  `mode='fixture'`, `target='fixture://slack/incoming-webhook'`, and the detail line
  "SLACK_WEBHOOK_URL is not set; payload recorded as a fixture receipt."

The Webhooks screen shows the connector mode as a pill reading `live` or `fixture`,
and every message row carries its own mode. The CI `n8n-e2e` job asserts the fixture
receipt exists, so the escalation path is proven end to end without a Slack workspace.

The six local connector surfaces from v1.1 keep their honesty sentence in the README
unchanged: they are deterministic local adapters, not claims of access to vendor
tenants.

## Consequences

- The message content is reviewable and testable without a credential; only the
  transport is missing, and the interface says which.
- Adding a real Slack workspace is one environment variable, and the same rows then
  read `live` with an HTTP status code.
- Nothing in the UI, the API or the tests ever implies a message reached Slack when it
  did not.
