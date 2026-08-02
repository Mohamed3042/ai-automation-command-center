from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from .intelligence import LLMAdapter, demand_forecast
from .reports import generate_report


ActionHandler = Callable[[object, dict, object, dict], dict]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_staged_orders(connection, context, step, config):
    rows = connection.execute("SELECT * FROM staged_orders WHERE status='pending' ORDER BY id").fetchall()
    required = {"order_id", "store", "amount", "currency", "status", "created_at"}
    validated = 0
    for row in rows:
        payload = json.loads(row["payload_json"])
        missing = required - payload.keys()
        if missing or float(payload["amount"]) <= 0:
            raise ValueError(f"Order {row['external_id']} failed {config.get('schema', 'order')} validation: {sorted(missing)}")
        connection.execute("UPDATE staged_orders SET status='validated', validated_at=? WHERE id=?", (utcnow_iso(), row["id"]))
        validated += 1
    return {"records": validated, "rows_read": len(rows), "rows_written": validated, "schema": config.get("schema", "order.v2")}


def normalize_orders(connection, context, step, config):
    rows = connection.execute("SELECT * FROM staged_orders WHERE status='validated' ORDER BY id").fetchall()
    written = 0
    for row in rows:
        payload = json.loads(row["payload_json"])
        cursor = connection.execute(
            "INSERT OR IGNORE INTO canonical_orders(staged_order_id,external_id,channel,store,amount,currency,order_status,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], payload["order_id"], row["channel"], payload["store"], float(payload["amount"]), payload["currency"], payload["status"], payload["created_at"]),
        )
        written += cursor.rowcount
        connection.execute("UPDATE staged_orders SET status='normalized', normalized_at=? WHERE id=?", (utcnow_iso(), row["id"]))
    return {"records": written, "rows_read": len(rows), "rows_written": written, "mapping": config.get("mapping", "canonical_order")}


def post_ledger_entries(connection, context, step, config):
    orders = connection.execute(
        "SELECT o.* FROM canonical_orders o LEFT JOIN ledger_entries l ON l.canonical_order_id=o.id WHERE l.id IS NULL ORDER BY o.id"
    ).fetchall()
    entries = 0
    now = utcnow_iso()
    for order in orders:
        for account, debit, credit in (("Accounts receivable", order["amount"], 0), ("Retail sales", 0, order["amount"])):
            entries += connection.execute(
                "INSERT OR IGNORE INTO ledger_entries(canonical_order_id,account,debit,credit,status,created_at) VALUES (?, ?, ?, ?, 'posted', ?)",
                (order["id"], account, debit, credit, now),
            ).rowcount
    return {"records": len(orders), "rows_read": len(orders), "rows_written": entries, "ledger": config.get("ledger", "sales"), "balanced_entries": entries}


def refresh_kpi_cache(connection, context, step, config):
    now = utcnow_iso()
    order_summary = connection.execute("SELECT COUNT(*) orders, COALESCE(SUM(amount),0) revenue FROM canonical_orders").fetchone()
    ledger_summary = connection.execute("SELECT COALESCE(SUM(debit),0) debit, COALESCE(SUM(credit),0) credit FROM ledger_entries").fetchone()
    metrics = {
        "ingested_orders": float(order_summary["orders"]),
        "ingested_revenue": float(order_summary["revenue"]),
        "ledger_variance": abs(float(ledger_summary["debit"]) - float(ledger_summary["credit"])),
    }
    for key, value in metrics.items():
        connection.execute(
            "INSERT INTO kpi_cache(metric_key,scope,value,refreshed_at) VALUES (?, 'all', ?, ?) ON CONFLICT(metric_key,scope) DO UPDATE SET value=excluded.value, refreshed_at=excluded.refreshed_at",
            (key, value, now),
        )
    return {"records": len(metrics), "rows_read": 2, "rows_written": len(metrics), "metrics": metrics}


def read_unclassified_messages(connection, context, step, config):
    rows = connection.execute("SELECT id FROM support_tickets WHERE category='Unclassified' ORDER BY id").fetchall()
    return {"records": len(rows), "rows_read": len(rows), "rows_written": 0, "ticket_ids": [row["id"] for row in rows]}


def classify_messages(connection, context, step, config):
    rows = connection.execute("SELECT * FROM support_tickets WHERE category='Unclassified' ORDER BY id").fetchall()
    adapter = context.get("llm_adapter") or LLMAdapter()
    updated = 0
    now = utcnow_iso()
    providers = set()
    for row in rows:
        result = adapter.classify(row["subject"], row["body"])
        connection.execute(
            "UPDATE support_tickets SET category=?, confidence=?, priority=?, classified_at=? WHERE id=?",
            (result["category"], result["confidence"], result["priority"], now, row["id"]),
        )
        providers.add(result["adapter"])
        updated += 1
    return {"records": updated, "rows_read": len(rows), "rows_written": updated, "providers": sorted(providers)}


def evaluate_rules(connection, context, step, config):
    if config.get("policy") == "support_sla":
        rows = connection.execute("SELECT * FROM support_tickets WHERE classified_at IS NOT NULL AND routed_queue IS NULL ORDER BY id").fetchall()
        updated = 0
        for row in rows:
            priority = "urgent" if row["category"] == "Payment" and "twice" in row["body"].lower() else row["priority"]
            connection.execute("UPDATE support_tickets SET priority=? WHERE id=?", (priority, row["id"]))
            updated += 1
        return {"records": updated, "rows_read": len(rows), "rows_written": updated, "policy": "support_sla"}
    risks = connection.execute(
        "SELECT *, ROUND(stock * 14.0 / forecast_units, 1) cover_days FROM products WHERE stock < reorder_point OR stock * 14.0 / forecast_units < 8 ORDER BY cover_days"
    ).fetchall()
    created = 0
    now = utcnow_iso()
    for product in risks:
        recommended = max(product["reorder_point"] * 2 - product["stock"], product["forecast_units"] - product["stock"])
        created += connection.execute(
            "INSERT OR IGNORE INTO replenishment_tasks(product_id,sku,recommended_qty,status,source_run_id,created_at) VALUES (?, ?, ?, 'open', ?, ?)",
            (product["id"], product["sku"], recommended, context["run_id"], now),
        ).rowcount
    return {"records": created, "rows_read": len(risks), "rows_written": created, "risks_evaluated": len(risks)}


def route_tickets(connection, context, step, config):
    rows = connection.execute("SELECT * FROM support_tickets WHERE classified_at IS NOT NULL AND routed_queue IS NULL ORDER BY id").fetchall()
    queues = {"Payment": "Billing", "Delivery": "Fulfilment", "Returns": "Returns", "Availability": "Store operations", "Account": "Customer accounts", "Product": "Product care"}
    for row in rows:
        connection.execute("UPDATE support_tickets SET routed_queue=? WHERE id=?", (queues.get(row["category"], "General support"), row["id"]))
    return {"records": len(rows), "rows_read": len(rows), "rows_written": len(rows), "queues": len(set(queues.values()))}


def snapshot_stock(connection, context, step, config):
    products = connection.execute("SELECT id,stock,forecast_units FROM products ORDER BY id").fetchall()
    now = utcnow_iso()
    connection.executemany(
        "INSERT INTO inventory_snapshots(product_id,stock,forecast_units,captured_at) VALUES (?, ?, ?, ?)",
        [(row["id"], row["stock"], row["forecast_units"], now) for row in products],
    )
    return {"records": len(products), "rows_read": len(products), "rows_written": len(products), "range": config.get("range")}


def refresh_forecast(connection, context, step, config):
    forecast = demand_forecast(connection, int(config.get("horizon_days", 14)))
    connection.execute(
        "INSERT INTO kpi_cache(metric_key,scope,value,refreshed_at) VALUES ('forecast_revenue','14d',?,?) ON CONFLICT(metric_key,scope) DO UPDATE SET value=excluded.value, refreshed_at=excluded.refreshed_at",
        (forecast["projected_revenue"], utcnow_iso()),
    )
    return {"records": 1, "rows_read": 28, "rows_written": 1, "projected_revenue": forecast["projected_revenue"], "horizon_days": forecast["horizon_days"]}


def send_buyer_messages(connection, context, step, config):
    tasks = connection.execute("SELECT * FROM replenishment_tasks WHERE status='open' ORDER BY id").fetchall()
    sent = 0
    now = utcnow_iso()
    for task in tasks:
        dedupe = f"replenishment-{task['id']}"
        sent += connection.execute(
            "INSERT OR IGNORE INTO outbound_messages(channel,recipient,template,payload_json,status,source_run_id,sent_at,dedupe_key) VALUES ('WhatsApp','Buyer queue',?,?, 'delivered',?,?,?)",
            (config.get("template", "inventory_risk"), json.dumps({"task_id": task["id"], "sku": task["sku"], "recommended_qty": task["recommended_qty"]}), context["run_id"], now, dedupe),
        ).rowcount
    return {"records": sent, "rows_read": len(tasks), "rows_written": sent, "channel": "WhatsApp"}


def aggregate_sales(connection, context, step, config):
    row = connection.execute("SELECT COUNT(*) rows, SUM(revenue) revenue, SUM(orders) orders, SUM(gross_margin) margin FROM sales_daily WHERE sale_date=(SELECT value FROM metadata WHERE key='demo_date')").fetchone()
    metrics = {"daily_revenue": row["revenue"] or 0, "daily_orders": row["orders"] or 0, "daily_margin": row["margin"] or 0, "daily_source_rows": row["rows"] or 0}
    now = utcnow_iso()
    for key, value in metrics.items():
        connection.execute("INSERT INTO kpi_cache(metric_key,scope,value,refreshed_at) VALUES (?, 'daily-close', ?, ?) ON CONFLICT(metric_key,scope) DO UPDATE SET value=excluded.value, refreshed_at=excluded.refreshed_at", (key, value, now))
    return {"records": len(metrics), "rows_read": int(row["rows"] or 0), "rows_written": len(metrics), "totals": metrics}


def reconcile_ledger(connection, context, step, config):
    row = connection.execute("SELECT COUNT(*) entries, COALESCE(SUM(debit),0) debit, COALESCE(SUM(credit),0) credit FROM ledger_entries WHERE status='posted'").fetchone()
    variance = round(abs(float(row["debit"]) - float(row["credit"])), 2)
    tolerance = float(config.get("tolerance", 0))
    if variance > tolerance:
        raise ValueError(f"Ledger variance {variance:.2f} exceeds tolerance {tolerance:.2f}")
    return {"records": int(row["entries"]), "rows_read": int(row["entries"]), "rows_written": 0, "debit": row["debit"], "credit": row["credit"], "variance": variance, "balanced": True}


def render_report(connection, context, step, config):
    report_type = config.get("type", "daily")
    artifacts = generate_report(connection, report_type, commit=False, generated_by=f"Workflow {context['run_key']}")
    return {"records": len(artifacts), "rows_read": sum(item["row_count"] for item in artifacts), "rows_written": len(artifacts), "artifacts": [item["path"] for item in artifacts]}


def record_report_distribution(connection, context, step, config):
    now = utcnow_iso()
    dedupe = f"report-distribution-{context['run_id']}"
    written = connection.execute(
        "INSERT OR IGNORE INTO outbound_messages(channel,recipient,template,payload_json,status,source_run_id,sent_at,dedupe_key) VALUES ('Email',?,'daily_close',?,'delivered',?,?,?)",
        (config.get("audience", "leadership"), json.dumps({"run_id": context["run_id"]}), context["run_id"], now, dedupe),
    ).rowcount
    return {"records": written, "rows_read": 0, "rows_written": written, "channel": "Email"}


ACTION_REGISTRY: dict[str, ActionHandler] = {
    "webhook.validate": validate_staged_orders,
    "transform.map": normalize_orders,
    "accounting.post": post_ledger_entries,
    "metrics.increment": refresh_kpi_cache,
    "message.receive": read_unclassified_messages,
    "ai.classify": classify_messages,
    "rules.evaluate": evaluate_rules,
    "ticket.assign": route_tickets,
    "sheet.read": snapshot_stock,
    "ai.forecast": refresh_forecast,
    "message.send": send_buyer_messages,
    "sales.aggregate": aggregate_sales,
    "accounting.reconcile": reconcile_ledger,
    "report.render": render_report,
    "email.send": record_report_distribution,
}


def execute_action(connection, context: dict, step, config: dict) -> dict:
    try:
        handler = ACTION_REGISTRY[step["action"]]
    except KeyError as exc:
        raise ValueError(f"Unknown workflow action: {step['action']}") from exc
    result = handler(connection, context, step, config)
    result.setdefault("records", 0)
    result.setdefault("rows_read", 0)
    result.setdefault("rows_written", 0)
    result["action"] = step["action"]
    result["connector"] = step["connector_slug"] or "relayops-core"
    return result
