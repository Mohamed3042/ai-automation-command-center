"""The versioned RelayOps API: one route table, one dispatcher, one envelope.

`ROUTES` is the single source of truth for what the server answers. The same
table is compared against `docs/openapi.v1.yaml` by
`tests/test_openapi_contract.py`, so a route that is added here and forgotten in
the specification fails the build.
"""
from __future__ import annotations

import re

from .. import logs, metrics
from ..db import ROOT
from .core import API_PREFIX, ApiError, Response
from .keys import authenticate
from . import v1

DOCS_DIR = ROOT / "docs"
STATIC_DIR = ROOT / "static"
SPEC_PATH = DOCS_DIR / "openapi.v1.yaml"
DOCS_PAGE = STATIC_DIR / "api-docs.html"

_PARAM = re.compile(r"\{([a-z_]+)\}")


class Route:
    __slots__ = ("method", "template", "pattern", "handler", "auth", "operation_id")

    def __init__(self, method: str, template: str, handler, auth: str = "bearer", operation_id: str | None = None) -> None:
        self.method = method
        self.template = template
        self.pattern = re.compile("^" + _PARAM.sub(lambda match: "(?P<{0}>[0-9]+)".format(match.group(1)), template) + "$")
        self.handler = handler
        self.auth = auth
        self.operation_id = operation_id or handler.__name__


class RequestContext:
    __slots__ = ("connection", "method", "path", "query", "body", "raw_body", "headers", "params", "principal", "request_id", "route", "db_path", "scheduler_running", "version")

    def __init__(self, **fields) -> None:
        for name in self.__slots__:
            setattr(self, name, fields.get(name))


def serve_spec(ctx) -> Response:
    if not SPEC_PATH.exists():  # pragma: no cover - only if docs are deleted
        raise ApiError("not_found", "OpenAPI document is not installed")
    return Response(200, body=SPEC_PATH.read_bytes(), media_type="application/yaml; charset=utf-8")


def serve_docs(ctx) -> Response:
    if not DOCS_PAGE.exists():  # pragma: no cover - only if static assets are deleted
        raise ApiError("not_found", "API documentation page is not installed")
    return Response(200, body=DOCS_PAGE.read_bytes(), media_type="text/html; charset=utf-8")


ROUTES = [
    Route("GET", "/api/v1/health", v1.health, "public", "getHealth"),
    Route("GET", "/api/v1/ready", v1.ready, "public", "getReadiness"),
    Route("GET", "/metrics", v1.prometheus_metrics, "public", "getMetrics"),
    Route("GET", "/api/v1/openapi.yaml", serve_spec, "public", "getOpenApiDocument"),
    Route("GET", "/api/v1/docs", serve_docs, "public", "getApiDocsPage"),

    Route("GET", "/api/v1/workflows", v1.list_workflows, "bearer", "listWorkflows"),
    Route("GET", "/api/v1/workflows/{workflow_id}", v1.get_workflow, "bearer", "getWorkflow"),
    Route("POST", "/api/v1/workflows/{workflow_id}/run", v1.run_workflow_route, "bearer", "runWorkflow"),
    Route("POST", "/api/v1/workflows/{workflow_id}/toggle", v1.toggle_workflow_route, "bearer", "toggleWorkflow"),

    Route("GET", "/api/v1/runs", v1.list_runs, "bearer", "listRuns"),
    Route("GET", "/api/v1/runs/{run_id}", v1.get_run, "bearer", "getRun"),
    Route("GET", "/api/v1/runs/{run_id}/attempts", v1.list_run_attempts, "bearer", "listRunAttempts"),

    Route("GET", "/api/v1/alerts", v1.list_alerts, "bearer", "listAlerts"),
    Route("GET", "/api/v1/alerts/{alert_id}", v1.get_alert, "bearer", "getAlert"),
    Route("POST", "/api/v1/alerts/{alert_id}/transition", v1.transition_alert_route, "bearer", "transitionAlert"),
    Route("POST", "/api/v1/alerts/mutes", v1.create_mute_route, "bearer", "createAlertMute"),

    Route("GET", "/api/v1/reports", v1.list_reports, "bearer", "listReports"),
    Route("POST", "/api/v1/reports/generate", v1.generate_report_route, "bearer", "generateReport"),
    Route("GET", "/api/v1/reports/{report_id}/download", v1.download_report_route, "bearer", "downloadReport"),

    Route("GET", "/api/v1/scheduler", v1.get_scheduler, "bearer", "getScheduler"),
    Route("GET", "/api/v1/scheduler/jobs", v1.list_scheduler_jobs, "bearer", "listSchedulerJobs"),

    Route("GET", "/api/v1/connectors", v1.list_connectors, "bearer", "listConnectors"),
    Route("GET", "/api/v1/connectors/messages", v1.list_connector_messages, "bearer", "listConnectorMessages"),
    Route("POST", "/api/v1/connectors/slack/notify", v1.slack_notify_route, "bearer", "notifySlack"),

    Route("POST", "/api/v1/webhooks/orders", v1._webhook_route("orders"), "signature", "receiveOrderWebhook"),
    Route("POST", "/api/v1/webhooks/tickets", v1._webhook_route("tickets"), "signature", "receiveTicketWebhook"),
    Route("GET", "/api/v1/webhooks/sources", v1.list_webhook_sources, "bearer", "listWebhookSources"),
    Route("GET", "/api/v1/webhooks/receipts", v1.list_webhook_receipts, "bearer", "listWebhookReceipts"),
    Route("GET", "/api/v1/webhooks/subscriptions", v1.list_subscriptions, "bearer", "listSubscriptions"),
    Route("POST", "/api/v1/webhooks/subscriptions", v1.create_subscription_route, "bearer", "createSubscription"),
    Route("POST", "/api/v1/webhooks/subscriptions/{subscription_id}/toggle", v1.toggle_subscription_route, "bearer", "toggleSubscription"),
    Route("GET", "/api/v1/webhooks/deliveries", v1.list_deliveries, "bearer", "listDeliveries"),
    Route("GET", "/api/v1/webhooks/deliveries/{delivery_id}", v1.get_delivery, "bearer", "getDelivery"),
    Route("POST", "/api/v1/webhooks/deliveries/{delivery_id}/retry", v1.retry_delivery_route, "bearer", "retryDelivery"),
]


def route_templates() -> dict:
    """`{path template: {method: operation_id}}` — what the server actually serves."""
    served: dict = {}
    for route in ROUTES:
        served.setdefault(route.template, {})[route.method] = route.operation_id
    return served


def handles(path: str) -> bool:
    """True for anything the versioned API owns, including its own 404s."""
    if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
        return True
    return any(route.pattern.match(path) for route in ROUTES)


def resolve(method: str, path: str):
    allowed = []
    for route in ROUTES:
        match = route.pattern.match(path)
        if not match:
            continue
        allowed.append(route.method)
        if route.method == method:
            return route, match.groupdict()
    if allowed:
        raise ApiError("method_not_allowed", "{0} is not allowed on {1}".format(method, path), {"allow": sorted(set(allowed))})
    raise ApiError("not_found", "No API route matches {0}".format(path))


def dispatch(ctx: RequestContext) -> Response:
    """Resolve, authenticate, run. Errors come back as the shared envelope."""
    template = "unmatched"
    try:
        route, params = resolve(ctx.method, ctx.path)
        template = route.template
        ctx.route = route
        ctx.params = params
        if route.auth == "bearer":
            ctx.principal = authenticate(ctx.connection, ctx.headers.get("Authorization") or ctx.headers.get("authorization") or "")
        else:
            ctx.principal = {"key_id": None, "name": route.auth, "scopes": "*"}
        response = route.handler(ctx)
    except ApiError as exc:
        response = Response(exc.status, exc.envelope(ctx.request_id))
    except ValueError as exc:
        response = Response(400, ApiError("bad_request", str(exc)).envelope(ctx.request_id))
    except Exception as exc:  # pragma: no cover - defensive
        logs.log("api.error", level="error", path=ctx.path, error="{0}: {1}".format(type(exc).__name__, exc))
        response = Response(500, ApiError("internal_error", "{0}: {1}".format(type(exc).__name__, exc)).envelope(ctx.request_id))
    metrics.increment("relayops_http_requests_total", {"method": ctx.method, "route": template, "status": str(response.status)})
    return response
