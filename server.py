#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app import __version__
from app.dashboard import alerts_payload, connectors_payload, dashboard_payload
from app.db import DEFAULT_DB_PATH, ROOT, get_connection, init_db, rows_as_dicts
from app.engine import evaluate_alert_rules, run_workflow, workflows_payload
from app.intelligence import intelligence_payload
from app.reports import REPORT_DIR, ensure_demo_reports, generate_report, reports_payload


STATIC_DIR = ROOT / "static"


class RelayOpsHandler(BaseHTTPRequestHandler):
    server_version = f"RelayOps/{__version__}"

    def log_message(self, format: str, *args) -> None:
        sys.stdout.write(f"[relayops] {self.address_string()} {format % args}\n")

    def _json(self, data, status: int = 200) -> None:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size == 0:
            return {}
        try:
            return json.loads(self.rfile.read(size))
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
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/api/health":
            self._json({"status": "ok", "service": "relayops", "version": __version__, "database": "connected", "llm_mode": "offline-rules"})
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
                elif path == "/api/meta":
                    metadata = {row["key"]: row["value"] for row in connection.execute("SELECT * FROM metadata")}
                    self._json({"version": __version__, "metadata": metadata, "runtime": {"python": sys.version.split()[0], "database": "SQLite", "external_keys_required": False}})
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
            alert_match = re.fullmatch(r"/api/alerts/(\d+)/ack", path)
            if workflow_match:
                result = run_workflow(connection, int(workflow_match.group(1)), "manual", bool(body.get("force_error", False)))
                self._json(result, 201)
            elif connector_match:
                connector_id = int(connector_match.group(1))
                connector = connection.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
                if not connector:
                    self._error(404, "Connector not found")
                    return
                connection.execute("UPDATE connectors SET last_sync=datetime('now'), latency_ms=MAX(90, latency_ms-75) WHERE id=?", (connector_id,))
                connection.commit()
                self._json({"status": "success", "connector": connector["name"], "records_synced": 140 + connector_id * 37})
            elif path == "/api/alerts/evaluate":
                self._json(evaluate_alert_rules(connection), 201)
            elif alert_match:
                alert_id = int(alert_match.group(1))
                updated = connection.execute("UPDATE alerts SET status='acknowledged', acknowledged_at=datetime('now') WHERE id=?", (alert_id,)).rowcount
                connection.commit()
                if not updated:
                    self._error(404, "Alert not found")
                else:
                    self._json({"status": "acknowledged", "alert_id": alert_id})
            elif path == "/api/reports/generate":
                self._json({"artifacts": generate_report(connection, body.get("report_type", "daily"))}, 201)
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


def create_server(host: str = "127.0.0.1", port: int = 4173, db_path: str | Path = DEFAULT_DB_PATH) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), RelayOpsHandler)
    server.db_path = str(db_path)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RelayOps AI Automation Command Center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--reset", action="store_true", help="rebuild the deterministic demo database")
    args = parser.parse_args()
    connection = init_db(args.db, reset=args.reset)
    ensure_demo_reports(connection)
    connection.close()
    server = create_server(args.host, args.port, args.db)
    print(f"RelayOps {__version__} running at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping RelayOps", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
