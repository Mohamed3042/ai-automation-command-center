from __future__ import annotations

from datetime import timedelta

from .db import DEMO_TODAY, rows_as_dicts


def _delta(current: float, previous: float) -> float:
    return round((current / previous - 1) * 100, 1) if previous else 0


def dashboard_payload(connection, days: int = 7, store: str = "All stores") -> dict:
    days = max(1, min(days, 42))
    end = DEMO_TODAY
    start = end - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    store_clause = "" if store == "All stores" else " AND store = ?"
    current_args = [start.isoformat(), end.isoformat()] + ([] if store == "All stores" else [store])
    previous_args = [previous_start.isoformat(), previous_end.isoformat()] + ([] if store == "All stores" else [store])
    metric_sql = f"SELECT SUM(revenue) revenue, SUM(orders) orders, SUM(gross_margin) margin, SUM(returns) returns FROM sales_daily WHERE sale_date BETWEEN ? AND ?{store_clause}"
    current = connection.execute(metric_sql, current_args).fetchone()
    previous = connection.execute(metric_sql, previous_args).fetchone()
    revenue = float(current["revenue"] or 0)
    orders = int(current["orders"] or 0)
    margin = float(current["margin"] or 0)
    returns = float(current["returns"] or 0)
    previous_revenue = float(previous["revenue"] or 0)
    previous_orders = int(previous["orders"] or 0)
    previous_margin_pct = float(previous["margin"] or 0) / previous_revenue * 100 if previous_revenue else 0
    timeline_args = [start.isoformat(), end.isoformat()] + ([] if store == "All stores" else [store])
    timeline = rows_as_dicts(connection.execute(
        f"SELECT sale_date date, ROUND(SUM(revenue), 2) revenue, SUM(orders) orders FROM sales_daily WHERE sale_date BETWEEN ? AND ?{store_clause} GROUP BY sale_date ORDER BY sale_date", timeline_args
    ).fetchall())
    categories = rows_as_dicts(connection.execute(
        f"SELECT category, ROUND(SUM(revenue), 2) revenue, SUM(orders) orders, ROUND(SUM(gross_margin) / SUM(revenue) * 100, 1) margin_pct FROM sales_daily WHERE sale_date BETWEEN ? AND ?{store_clause} GROUP BY category ORDER BY revenue DESC", current_args
    ).fetchall())
    stores = rows_as_dicts(connection.execute(
        "SELECT store, ROUND(SUM(revenue), 2) revenue, SUM(orders) orders, ROUND(SUM(gross_margin) / SUM(revenue) * 100, 1) margin_pct, ROUND(SUM(returns) / SUM(revenue) * 100, 2) return_rate FROM sales_daily WHERE sale_date BETWEEN ? AND ? GROUP BY store ORDER BY revenue DESC",
        (start.isoformat(), end.isoformat()),
    ).fetchall())
    events = rows_as_dicts(connection.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 6").fetchall())
    open_alerts = connection.execute("SELECT COUNT(*) count FROM alerts WHERE status IN ('open','investigating')").fetchone()["count"]
    workflow_quality = connection.execute("SELECT SUM(runs_30d * success_rate) / SUM(runs_30d) success_rate, SUM(runs_30d) runs FROM workflows").fetchone()
    return {
        "scope": {"days": days, "store": store, "period_start": start.isoformat(), "period_end": end.isoformat()},
        "stores_filter": ["All stores"] + [row["store"] for row in stores],
        "kpis": [
            {"id": "revenue", "label": "Net revenue", "value": round(revenue - returns, 2), "format": "currency", "delta": _delta(revenue, previous_revenue), "note": f"vs prior {days} days"},
            {"id": "orders", "label": "Orders", "value": orders, "format": "integer", "delta": _delta(orders, previous_orders), "note": f"across {6 if store == 'All stores' else 1} channels / stores"},
            {"id": "margin", "label": "Gross margin", "value": round(margin / revenue * 100, 1) if revenue else 0, "format": "percent", "delta": round((margin / revenue * 100 if revenue else 0) - previous_margin_pct, 1), "note": "percentage-point movement"},
            {"id": "automation", "label": "Automation success", "value": round(workflow_quality["success_rate"], 1), "format": "percent", "delta": 0.8, "note": f"{workflow_quality['runs']:,} runs · 30 days"},
        ],
        "timeline": timeline,
        "categories": categories,
        "stores": stores,
        "events": events,
        "open_alerts": open_alerts,
        "data_freshness": "18 sec ago",
    }


def connectors_payload(connection) -> dict:
    connectors = rows_as_dicts(connection.execute("SELECT * FROM connectors ORDER BY id").fetchall())
    healthy = sum(item["status"] == "healthy" for item in connectors)
    return {
        "connectors": connectors,
        "metrics": {
            "connected": len(connectors),
            "healthy": healthy,
            "events_today": sum(item["records_today"] for item in connectors),
            "average_uptime": round(sum(item["uptime"] for item in connectors) / len(connectors), 2),
        },
        "contracts": ["REST", "webhooks", "Graph API", "Cloud API", "SuiteTalk", "Sheets API"],
    }


def alerts_payload(connection) -> dict:
    alerts = rows_as_dicts(connection.execute("SELECT * FROM alerts ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, created_at DESC").fetchall())
    deliveries = rows_as_dicts(connection.execute(
        "SELECT d.*, a.title alert_title, a.severity FROM alert_deliveries d JOIN alerts a ON a.id=d.alert_id ORDER BY d.sent_at DESC, d.id DESC LIMIT 24"
    ).fetchall())
    counts = {severity: sum(item["severity"] == severity and item["status"] != "acknowledged" for item in alerts) for severity in ("critical", "high", "medium", "low")}
    return {
        "alerts": alerts,
        "deliveries": deliveries,
        "counts": counts,
        "policies": [
            {"severity": "Critical", "ack": "15 min", "channels": "In-app → WhatsApp → Email", "owner": "Duty manager"},
            {"severity": "High", "ack": "30 min", "channels": "In-app → WhatsApp", "owner": "Functional lead"},
            {"severity": "Medium", "ack": "60 min", "channels": "In-app", "owner": "Operations queue"},
            {"severity": "Low", "ack": "Next business day", "channels": "In-app digest", "owner": "Source owner"},
        ],
    }
