# 0002 - The OpenAPI document is hand-authored and tested against the router

Date: 2026-09-05 - Status: accepted

## Context

A versioned API needs a machine-readable contract. Frameworks usually generate one
from decorated handlers; RelayOps has no framework (ADR 0001), so the options were
to generate a thin document from the route table, or to write a full one by hand.

A generated document from a standard-library router would carry paths and methods
and almost nothing else - no examples, no error shapes, no explanation of what a
webhook signature is. That is the part a reader actually needs.

## Decision

`docs/openapi.v1.yaml` is written by hand, with descriptions, schemas, examples and
error responses, and it is **the** contract: served verbatim at
`/api/v1/openapi.yaml` and rendered by the vendored Redoc page at `/api/v1/docs`.

The risk of a hand-authored document is drift, so drift is what the tests attack:

- every served route template appears in the document, and every documented path is
  served - compared in both directions;
- methods match per path, and every `operationId` matches the route table and is unique;
- routes the router marks public declare `security: []`, and the rest do not;
- every `$ref` resolves and every operation documents at least one response.

`tests/test_openapi_contract.py` reads the document with `tests/support/yamlmini.py`,
a small reader for the exact YAML subset the document uses, so the gate runs in the
dependency-free base CI job. The reader is itself under test: when PyYAML is
installed (the `spec-tools` job) the parse is compared against it, and the document
is validated against the OpenAPI 3.1 schema by `openapi-spec-validator`. That job
fails if either check is skipped.

## Consequences

- Adding a route means editing two files. That is the cost, and the tests make it a
  build failure rather than a silent lie in the documentation.
- The document can say things a generator never could - the signing recipe, the
  replay semantics, the alias list the order mapper accepts.
- `yamlmini` is a small piece of code that must stay honest; it raises rather than
  guesses on anything outside its subset, and the PyYAML comparison is the gate.
