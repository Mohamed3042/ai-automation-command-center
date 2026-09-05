# Vendored third-party assets

| File | Version | Source | SHA-256 | License |
|---|---|---|---|---|
| `redoc.standalone.js` | Redoc 2.5.0 | `https://cdn.redocly.com/redoc/v2.5.0/bundles/redoc.standalone.js` | `0ec05be285ac885a330289b02f470e1bdbd2b6b3223a9fa213f24bf805a851d1` | MIT |

Vendored so `/api/v1/docs` renders the OpenAPI document with no network access and
no CDN dependency. It is a browser asset only: the RelayOps server runtime still
has no third-party dependency, and `tests/test_runtime_boundary.py` proves it.
