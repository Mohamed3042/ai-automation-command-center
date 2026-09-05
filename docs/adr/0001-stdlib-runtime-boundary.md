# 0001 - The runtime stays standard library only

Date: 2026-09-05 - Status: accepted

## Context

`requirements.txt` has said "No third-party Python packages required. Python 3.9+
standard library only." since v1.0, and that sentence is a large part of why anyone
can run this project at all: clone, `python3 server.py --reset`, done. v1.2 adds a
REST API, an OpenAPI document, HMAC signing, outbound delivery with retries,
Prometheus metrics and structured logs - every one of which has an obvious library.

Taking any of them would quietly make the README's headline claim false.

## Decision

The application (`app/` and `server.py`) imports nothing outside the standard
library. Development and CI tooling may use packages, and they live in a separate
`requirements-dev.txt` (`openapi-spec-validator`, `PyYAML`).

The boundary is enforced, not promised: `tests/test_runtime_boundary.py` parses
every import in the runtime files and fails if one resolves outside the standard
library. It runs in the base CI job, which installs nothing.

The concrete substitutions:

| Would normally be | Is instead |
|---|---|
| FastAPI / Flask | `http.server.ThreadingHTTPServer` with a route table and a dispatcher |
| pydantic | explicit validation that raises `ApiError` with a code and a message |
| passlib / bcrypt | `hashlib.pbkdf2_hmac("sha256", ..., 200_000)` with a per-key salt |
| requests | `urllib.request` with an injectable opener, which also makes delivery testable |
| prometheus_client | a counter dict behind a lock and a text renderer |
| structlog | `json.dumps` of one record per line, with a `contextvars` request id |
| PyYAML at runtime | the document is served as bytes; only the tests parse it |

## Consequences

- The boundary is visible in the code: no dependency injection framework, no ORM,
  no serialisation layer. A reviewer can read the whole request path.
- Some things are deliberately simpler than a framework would give: no async, no
  content negotiation, no generated schema. The OpenAPI document is hand-authored
  (ADR 0002) because generating it would need a framework.
- The docs page vendors Redoc as a **browser** asset (`static/vendor/`). That is not
  a runtime dependency, and the boundary test proves it.
- Adding a package to `app/` is a one-line change that fails CI, which is the point:
  the promise cannot rot silently.
