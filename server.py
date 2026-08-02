#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__
from app.alerts import create_mute_rule, set_mute_rule_active, transition_alert
from app.dashboard import alerts_payload, connectors_payload, dashboard_payload
from app.db import DEFAULT_DB_PATH, ROOT, get_connection, init_db
from app.engine import FailureInjector, evaluate_alert_rules, run_workflow, save_workflow, toggle_workflow, workflows_payload
from app.intelligence import LLMAdapter, intelligence_payload
from app.reports import REPORT_DIR, generate_report, reports_payload
from app.scheduler import RelayScheduler, scheduler_payload


STATIC_DIR = ROOT / "static"


class RelayOpsHandler(BaseHTTPRequestHandler):
    server_version = f"RelayOps/{__version__}"

    def log_message(self, format: str, *args) -> None:
        sys.stdout.write(f"[relayops] {self.address_string()} {format % args}\n")

    def _json(self, data, status: int = 200) -> None:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(size))
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
        try:
            self.send_response(200)
            self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
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

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        connection = get_connection(self.server.db_path)
        try:
            body = self._body()
            workflow_match = re.fullmatch(r"/api/workflows/(\d+)/run", path)
            connector_match = re.fullmatch(r"/api/connectors/(\d+)/sync", path)
            workflow_toggle_match = re.fullmatch(r"/api/workflows/(\d+)/toggle", path)
            alert_transition_match = re.fullmatch(r"/api/alerts/(\d+)/transition", path)
            mute_toggle_match = re.fullmatch(r"/api/alerts/mutes/(\d+)/toggle", path)
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
            elif path == "/api/reports/generate":
                self._json({"artifacts": generate_report(connection, body.get("report_type", "daily"))}, 201)
            elif path == "/api/scheduler/tick":
                requested_now = datetime.fromisoformat(body["now"].replace("Z", "+00:00")) if body.get("now") else None
                self._json(self.server.scheduler.tick(requested_now), 201)
            elif path == "/api/intelligence/refresh":
                self._json({"status": "refreshed", "intelligence": intelligence_payload(connection)}, 201)
            else:
                self._error(404, "API endpoint not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"Internal error: {exc}")
        finally:
            connection.close()

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RelayOps AI Automation Command Center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--reset", action="store_true", help="rebuild the deterministic demo database")
    args = parser.parse_args()
    connection = init_db(args.db, reset=args.reset)
    connection.close()
    server = create_server(args.host, args.port, args.db)
    server.scheduler.start()
    print(f"RelayOps {__version__} running at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping RelayOps", flush=True)
    finally:
        server.scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
