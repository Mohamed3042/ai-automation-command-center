#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__, api, logs
from app.alerts import create_mute_rule, set_mute_rule_active, transition_alert
from app.api import keys as api_keys
from app.dashboard import alerts_payload, connectors_payload, dashboard_payload
from app.db import DEFAULT_DB_PATH, ROOT, get_connection, init_db
from app.engine import FailureInjector, evaluate_alert_rules, run_workflow, save_workflow, toggle_workflow, workflows_payload
from app.events import bootstrap_from_env as bootstrap_subscription
from app.events import create_subscription, retry_delivery, set_subscription_active
from app.intelligence import LLMAdapter, intelligence_payload
from app.reports import REPORT_DIR, generate_report, reports_payload
from app.scheduler import RelayScheduler, scheduler_payload
from app.webhooks import screen_payload


STATIC_DIR = ROOT / "static"
# Windows reads MIME types from the registry, where .js is sometimes text/plain;
# a browser then refuses the vendored Redoc bundle. Pin the ones we serve.
for _extension, _mime in ((".js", "text/javascript"), (".css", "text/css"), (".svg", "image/svg+xml"), (".json", "application/json"), (".yaml", "application/yaml")):
    mimetypes.add_type(_mime, _extension)
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"))


class RelayOpsHandler(BaseHTTPRequestHandler):
    server_version = f"RelayOps/{__version__}"

    def log_message(self, format: str, *args) -> None:
        """Suppressed: every request is logged as one structured line in _handle."""

    def log_error(self, format: str, *args) -> None:
        logs.log("http.error", level="warning", detail=format % args, client=self.address_string())

    def _send(self, status: int, body: bytes, media_type: str, extra_headers=()) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", logs.current_request_id())
            for header, value in SECURITY_HEADERS:
                self.send_header(header, value)
            for header, value in extra_headers:
                self.send_header(header, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _json(self, data, status: int = 200) -> None:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send(status, encoded, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status)

    def _body(self) -> dict:
        if not self._raw_body:
            return {}
        try:
            body = json.loads(self._raw_body)
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            return body
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must be valid JSON")

    def _serve_file(self, path: Path, download: bool = False) -> None:
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            self._error(404, "File not found")
            return
        allowed_root = REPORT_DIR.resolve() if download else STATIC_DIR.resolve()
        if allowed_root not in path.parents and path != allowed_root:
            self._error(403, "File path is outside the allowed root")
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        media_type = f"{mime}; charset=utf-8" if mime.startswith("text/") else mime
        extra = ((("Content-Disposition", f'attachment; filename="{path.name}"'),) if download else ())
        self._send(200, content, media_type, extra)

    # ------------------------------------------------------------------ routing

    def _handle(self, method: str) -> None:
        logs.set_request_id(logs.new_request_id())
        started = time.perf_counter()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        size = int(self.headers.get("Content-Length", "0") or "0")
        self._raw_body = self.rfile.read(size) if size else b""
        self._status = 500
        try:
            if api.handles(path):
                self._dispatch_v1(method, path, parse_qs(parsed.query))
            elif method == "GET":
                self._legacy_get(path, parse_qs(parsed.query))
            elif method == "POST":
                self._legacy_post(path)
            elif method == "PUT":
                self._legacy_put(path)
            else:  # pragma: no cover - BaseHTTPRequestHandler rejects other verbs first
                self._error(405, "Method not allowed")
        finally:
            logs.log(
                "http.request",
                method=method,
                path=path,
                status=self._status,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                client=self.address_string(),
            )

    def send_response(self, code, message=None):  # noqa: D102 - record the status for the access log
        self._status = code
        super().send_response(code, message)

    def _dispatch_v1(self, method: str, path: str, query: dict) -> None:
        connection = get_connection(self.server.db_path)
        try:
            body = None
            if self._raw_body:
                try:
                    body = json.loads(self._raw_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
            context = api.RequestContext(
                connection=connection,
                method=method,
                path=path,
                query=query,
                body=body,
                raw_body=self._raw_body,
                headers=self.headers,
                params={},
                principal={},
                request_id=logs.current_request_id(),
                db_path=self.server.db_path,
                scheduler_running=self.server.scheduler.running,
                version=__version__,
            )
            response = api.dispatch(context)
            extra = tuple(response.headers.items())
            self._send(response.status, response.encoded(), response.media_type, extra)
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    # ------------------------------------------------------------------ v1.1 surface

    def _legacy_get(self, path: str, query: dict) -> None:
        if path == "/api/health":
            try:
                health_connection = get_connection(self.server.db_path)
                health_connection.execute("SELECT 1").fetchone()
                health_connection.close()
                self._json({"status": "ok", "service": "relayops", "version": __version__, "database": "connected", "scheduler": "running" if self.server.scheduler.running else "stopped", "llm": LLMAdapter().status()})
            except Exception as exc:
                self._json({"status": "degraded", "service": "relayops", "version": __version__, "database": "unavailable", "error": str(exc)[:160]}, 503)
            return
        connection = None
        try:
            if path.startswith("/api/"):
                connection = get_connection(self.server.db_path)
                if path == "/api/dashboard":
                    days = int(query.get("days", ["7"])[0])
                    store = query.get("store", ["All stores"])[0]
                    self._json(dashboard_payload(connection, days, store))
                elif path == "/api/connectors":
                    self._json(connectors_payload(connection))
                elif path == "/api/workflows":
                    self._json(workflows_payload(connection))
                elif path == "/api/intelligence":
                    self._json(intelligence_payload(connection))
                elif path == "/api/reports":
                    self._json(reports_payload(connection))
                elif path == "/api/alerts":
                    self._json(alerts_payload(connection))
                elif path == "/api/scheduler":
                    self._json(scheduler_payload(connection))
                elif path == "/api/webhooks":
                    self._json(screen_payload(connection))
                elif path == "/api/meta":
                    metadata = {row["key"]: row["value"] for row in connection.execute("SELECT * FROM metadata")}
                    self._json({"version": __version__, "metadata": metadata, "runtime": {"python": sys.version.split()[0], "database": "SQLite", "external_keys_required": False, "hosted_llm_optional": True}})
                elif path.startswith("/api/reports/download/"):
                    filename = path.rsplit("/", 1)[-1]
                    self._serve_file(REPORT_DIR / filename, download=True)
                else:
                    self._error(404, "API endpoint not found")
                return
            requested = "index.html" if path in ("/", "") else path.lstrip("/")
            candidate = STATIC_DIR / requested
            if not candidate.exists() or candidate.is_dir():
                candidate = STATIC_DIR / "index.html"
            self._serve_file(candidate)
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"Internal error: {exc}")
        finally:
            if connection:
                connection.close()

    def _legacy_post(self, path: str) -> None:
        connection = get_connection(self.server.db_path)
        try:
            body = self._body()
            workflow_match = re.fullmatch(r"/api/workflows/(\d+)/run", path)
            connector_match = re.fullmatch(r"/api/connectors/(\d+)/sync", path)
            workflow_toggle_match = re.fullmatch(r"/api/workflows/(\d+)/toggle", path)
            alert_transition_match = re.fullmatch(r"/api/alerts/(\d+)/transition", path)
            mute_toggle_match = re.fullmatch(r"/api/alerts/mutes/(\d+)/toggle", path)
            subscription_toggle_match = re.fullmatch(r"/api/webhooks/subscriptions/(\d+)/toggle", path)
            delivery_retry_match = re.fullmatch(r"/api/webhooks/deliveries/(\d+)/retry", path)
            if workflow_match:
                injector = FailureInjector.from_payload(body.get("failure_injection"))
                result = run_workflow(connection, int(workflow_match.group(1)), "manual", injector)
                self._json(result, 201)
            elif path == "/api/workflows":
                self._json(save_workflow(connection, body), 201)
            elif workflow_toggle_match:
                self._json(toggle_workflow(connection, int(workflow_toggle_match.group(1)), bool(body.get("active"))))
            elif connector_match:
                connector_id = int(connector_match.group(1))
                connector = connection.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
                if not connector:
                    self._error(404, "Connector not found")
                    return
                synced_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                connection.execute("UPDATE connectors SET last_sync=? WHERE id=?", (synced_at, connector_id))
                connection.execute(
                    "INSERT INTO audit_events(event_type,title,detail,actor,created_at) VALUES ('connector',?,?, 'Ops operator',?)",
                    (f"{connector['name']} checkpoint refreshed", f"Read and checkpointed {connector['records_today']} persisted source record counters.", synced_at),
                )
                connection.commit()
                self._json({"status": "success", "connector": connector["name"], "records_synced": connector["records_today"], "synced_at": synced_at, "mode": "SQLite checkpoint"})
            elif path == "/api/alerts/evaluate":
                self._json(evaluate_alert_rules(connection), 201)
            elif alert_transition_match:
                self._json(transition_alert(connection, int(alert_transition_match.group(1)), str(body.get("status", "")), note=str(body.get("note", ""))))
            elif path == "/api/alerts/mutes":
                self._json(create_mute_rule(connection, str(body.get("source_pattern", "*")), str(body.get("severity", "*")), int(body.get("duration_minutes", 60)), str(body.get("reason", ""))), 201)
            elif mute_toggle_match:
                self._json(set_mute_rule_active(connection, int(mute_toggle_match.group(1)), bool(body.get("active"))))
            elif path == "/api/webhooks/subscriptions":
                self._json(create_subscription(connection, str(body.get("name", "")), str(body.get("url", "")), str(body.get("secret", "")), str(body.get("event_filter", "*")), bool(body.get("active", True))), 201)
            elif subscription_toggle_match:
                self._json(set_subscription_active(connection, int(subscription_toggle_match.group(1)), bool(body.get("active"))))
            elif delivery_retry_match:
                self._json(retry_delivery(connection, int(delivery_retry_match.group(1))))
            elif path == "/api/reports/generate":
                self._json({"artifacts": generate_report(connection, body.get("report_type", "daily"))}, 201)
            elif path == "/api/scheduler/tick":
                requested_now = datetime.fromisoformat(body["now"].replace("Z", "+00:00")) if body.get("now") else None
                self._json(self.server.scheduler.tick(requested_now), 201)
            elif path == "/api/intelligence/refresh":
                self._json({"status": "refreshed", "intelligence": intelligence_payload(connection)}, 201)
            else:
                self._error(404, "API endpoint not found")
        except api.ApiError as exc:
            self._error(exc.status, exc.message)
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"Internal error: {exc}")
        finally:
            connection.close()

    def _legacy_put(self, path: str) -> None:
        match = re.fullmatch(r"/api/workflows/(\d+)", path)
        if not match:
            self._error(404, "API endpoint not found")
            return
        connection = get_connection(self.server.db_path)
        try:
            self._json(save_workflow(connection, self._body(), int(match.group(1))))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"Internal error: {exc}")
        finally:
            connection.close()


def create_server(host: str = "127.0.0.1", port: int = 4173, db_path: str | Path = DEFAULT_DB_PATH) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), RelayOpsHandler)
    server.db_path = str(db_path)
    server.scheduler = RelayScheduler(server.db_path)
    return server


def _keys_command(args) -> int:
    connection = init_db(args.db)
    try:
        if args.keys_command == "create":
            record = api_keys.create_key(connection, args.name, args.scopes)
            print("API key created. The token is shown once and is not recoverable:")
            print(f"  key_id : {record['key_id']}")
            print(f"  name   : {record['name']}")
            print(f"  scopes : {record['scopes']}")
            print(f"  token  : {record['token']}")
            print("Use it as: Authorization: Bearer <token>")
        elif args.keys_command == "revoke":
            api_keys.revoke_key(connection, args.key_id)
            print(f"API key {args.key_id} revoked.")
        else:
            rows = api_keys.list_keys(connection)
            if not rows:
                print("No API keys. Create one with: python server.py keys create --name <name>")
            for row in rows:
                state = "revoked" if row["revoked_at"] else "active"
                print(f"{row['key_id']}  {state:<7}  {row['name']}  [{row['scopes']}]  last used {row['last_used_at'] or 'never'}")
    except api.ApiError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2
    finally:
        connection.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RelayOps AI Automation Command Center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--reset", action="store_true", help="rebuild the deterministic demo database")
    return parser


def build_keys_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server.py keys", description="Manage scoped RelayOps API keys")
    parser.add_argument("keys_command", choices=("create", "list", "revoke"))
    parser.add_argument("--name", default="", help="human label for the key")
    parser.add_argument("--scopes", default="*", help="space or comma separated scopes, or * for all")
    parser.add_argument("--key-id", dest="key_id", default="", help="key id to revoke")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    return parser


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "keys":
        raise SystemExit(_keys_command(build_keys_parser().parse_args(sys.argv[2:])))
    args = build_parser().parse_args()
    connection = init_db(args.db, reset=args.reset)
    bootstrap_key = api_keys.bootstrap_from_env(connection)
    bootstrap_sub = bootstrap_subscription(connection)
    connection.close()
    server = create_server(args.host, args.port, args.db)
    server.scheduler.start()
    logs.log(
        "server.started",
        version=__version__,
        url=f"http://{args.host}:{args.port}",
        api_base="/api/v1",
        bootstrap_api_key=None if bootstrap_key is None else bootstrap_key["status"],
        bootstrap_subscription=None if bootstrap_sub is None else bootstrap_sub["status"],
    )
    print(f"RelayOps {__version__} running at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logs.log("server.stopping")
    finally:
        server.scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
