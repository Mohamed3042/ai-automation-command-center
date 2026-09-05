"""One JSON object per line on stdout, carrying the request id that ties them together.

`RELAYOPS_LOG_FORMAT=text` restores the v1.1 human line for local debugging; the
default is JSON so a container log driver can index it.
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone

_REQUEST_ID = contextvars.ContextVar("relayops_request_id", default="")
_WRITE_LOCK = threading.Lock()


def json_enabled() -> bool:
    return os.environ.get("RELAYOPS_LOG_FORMAT", "json").lower() != "text"


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def set_request_id(request_id: str) -> str:
    _REQUEST_ID.set(request_id)
    return request_id


def current_request_id() -> str:
    return _REQUEST_ID.get()


def _write(line: str) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(event: str, level: str = "info", **fields) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level,
        "service": "relayops",
        "event": event,
    }
    request_id = current_request_id()
    if request_id:
        record["request_id"] = request_id
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    if json_enabled():
        _write(json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str))
    else:
        extra = " ".join("{0}={1}".format(key, value) for key, value in fields.items() if value is not None)
        _write("[relayops] {0} {1} {2}".format(level, event, extra).rstrip())
    return record
