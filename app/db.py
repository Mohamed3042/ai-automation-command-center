from __future__ import annotations

import json
import math
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "command_center.db"
DEMO_TODAY = date(2026, 8, 2)


def get_connection(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or os.environ.get("RELAYOPS_DB", DEFAULT_DB_PATH))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connectors (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    system TEXT NOT NULL,
    status TEXT NOT NULL,
    color TEXT NOT NULL,
    uptime REAL NOT NULL,
    last_sync TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    records_today INTEGER NOT NULL,
    error_rate REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'demo adapter'
);

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger TEXT NOT NULL,
    active INTEGER NOT NULL,
    runs_30d INTEGER NOT NULL,
    success_rate REAL NOT NULL,
    avg_duration_ms INTEGER NOT NULL,
    owner TEXT NOT NULL,
    failure_policy TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    connector_slug TEXT,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    run_key TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    records_processed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES workflow_steps(id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    output_json TEXT NOT NULL,
    error TEXT,
    attempt INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sales_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_date TEXT NOT NULL,
    store TEXT NOT NULL,
    category TEXT NOT NULL,
    revenue REAL NOT NULL,
    orders INTEGER NOT NULL,
    gross_margin REAL NOT NULL,
    returns REAL NOT NULL,
    UNIQUE(sale_date, store, category)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    stock INTEGER NOT NULL,
    reorder_point INTEGER NOT NULL,
    units_30d INTEGER NOT NULL,
    forecast_units INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY,
    customer TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    channel TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response_minutes INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    escalates_at TEXT,
    acknowledged_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_schedules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    report_type TEXT NOT NULL,
    frequency TEXT NOT NULL,
    next_run TEXT NOT NULL,
    format TEXT NOT NULL,
    recipients TEXT NOT NULL,
    active INTEGER NOT NULL,
    last_run TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    report_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    format TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


CONNECTORS = [
    (1, "square-pos", "Square POS", "Point of sale", "POS", "healthy", "#6558f5", 99.99, "2026-08-02T09:44:31Z", 182, 18420, 0.02, "seeded REST adapter"),
    (2, "shopify", "Shopify Plus", "E-commerce", "Commerce", "healthy", "#14b87a", 99.97, "2026-08-02T09:44:18Z", 226, 6842, 0.05, "seeded webhook adapter"),
    (3, "netsuite", "Oracle NetSuite", "Accounting", "Finance", "degraded", "#f59e0b", 99.42, "2026-08-02T09:37:02Z", 1840, 1251, 1.82, "seeded SuiteTalk adapter"),
    (4, "microsoft-mail", "Microsoft 365 Mail", "Email", "Support", "healthy", "#0ea5e9", 99.95, "2026-08-02T09:43:51Z", 311, 392, 0.08, "seeded Graph adapter"),
    (5, "whatsapp", "WhatsApp Business", "Messaging", "Support", "healthy", "#25d366", 99.91, "2026-08-02T09:44:09Z", 268, 1147, 0.11, "seeded Cloud API adapter"),
    (6, "google-sheets", "Google Sheets", "Spreadsheets", "Operations", "healthy", "#3ba272", 99.88, "2026-08-02T09:42:47Z", 419, 2284, 0.14, "seeded Sheets API adapter"),
]


WORKFLOWS = [
    (1, "Omnichannel order sync", "Normalizes POS and web orders, posts finance entries, and updates the live KPI layer.", "Webhook · every order", 1, 12842, 99.72, 1240, "Commerce Ops", "Retry 3× · exponential backoff · escalate"),
    (2, "Support triage & routing", "Classifies inbound email and messages, scores urgency, and routes cases to the right queue.", "New email or message", 1, 1064, 98.96, 890, "Customer Care", "Retry 2× · dead-letter queue"),
    (3, "Inventory risk monitor", "Combines sell-through and demand forecasts, creates replenishment tasks, and alerts buyers.", "Every 30 minutes", 1, 1410, 99.36, 2180, "Merchandising", "Retry 3× · alert on-call"),
    (4, "Daily finance close", "Reconciles channel totals, verifies ledger balance, and renders the daily executive report.", "Daily · 23:30", 1, 30, 96.67, 6840, "Finance", "Pause branch · notify controller"),
]


WORKFLOW_STEPS = [
    (1, 1, 1, "Receive order event", "webhook.validate", "shopify", '{"schema":"order.v2"}'),
    (2, 1, 2, "Normalize order", "transform.map", None, '{"mapping":"canonical_order"}'),
    (3, 1, 3, "Post journal entry", "accounting.post", "netsuite", '{"ledger":"sales"}'),
    (4, 1, 4, "Refresh KPI cache", "metrics.increment", None, '{"scope":"store,channel"}'),
    (5, 2, 1, "Ingest conversation", "message.receive", "microsoft-mail", '{}'),
    (6, 2, 2, "Classify intent", "ai.classify", None, '{"fallback":"keyword_rules_v3"}'),
    (7, 2, 3, "Score priority", "rules.evaluate", None, '{"policy":"support_sla"}'),
    (8, 2, 4, "Route to queue", "ticket.assign", None, '{"queues":5}'),
    (9, 3, 1, "Read stock levels", "sheet.read", "google-sheets", '{"range":"Inventory!A:H"}'),
    (10, 3, 2, "Forecast demand", "ai.forecast", None, '{"horizon_days":14}'),
    (11, 3, 3, "Evaluate reorder risk", "rules.evaluate", None, '{"threshold":"cover_days < 8"}'),
    (12, 3, 4, "Notify buyer", "message.send", "whatsapp", '{"template":"inventory_risk"}'),
    (13, 4, 1, "Aggregate channels", "sales.aggregate", "square-pos", '{"window":"day"}'),
    (14, 4, 2, "Reconcile ledger", "accounting.reconcile", "netsuite", '{"tolerance":25}'),
    (15, 4, 3, "Generate report", "report.render", None, '{"type":"daily_close"}'),
    (16, 4, 4, "Distribute summary", "email.send", "microsoft-mail", '{"audience":"leadership"}'),
]


def init_db(path: str | Path | None = None, reset: bool = False) -> sqlite3.Connection:
    db_path = Path(path or os.environ.get("RELAYOPS_DB", DEFAULT_DB_PATH))
    if reset and db_path.exists():
        db_path.unlink()
    connection = get_connection(db_path)
    connection.executescript(SCHEMA)
    exists = connection.execute("SELECT value FROM metadata WHERE key='seed_version'").fetchone()
    if not exists:
        _seed(connection)
    return connection


def _seed(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO connectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        CONNECTORS,
    )
    connection.executemany(
        "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        WORKFLOWS,
    )
    connection.executemany(
        "INSERT INTO workflow_steps VALUES (?, ?, ?, ?, ?, ?, ?)",
        WORKFLOW_STEPS,
    )
    _seed_sales(connection)
    _seed_products(connection)
    _seed_support(connection)
    _seed_workflow_runs(connection)
    _seed_alerts(connection)
    _seed_report_schedules(connection)
    _seed_audit(connection)
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("seed_version", "1.0.0"),
            ("demo_date", DEMO_TODAY.isoformat()),
            ("business_name", "Northstar Retail Group"),
            ("currency", "USD"),
        ],
    )
    connection.commit()


def _seed_sales(connection: sqlite3.Connection) -> None:
    rng = random.Random(7719)
    stores = {
        "Downtown Flagship": 1.20,
        "Riverside": 1.05,
        "Westgate": 0.92,
        "Airport": 0.82,
        "North Hills": 0.78,
        "Online": 1.28,
    }
    categories = {
        "Electronics": (10500, 0.238, 242),
        "Apparel": (8000, 0.462, 74),
        "Home": (6500, 0.394, 116),
        "Grocery": (6000, 0.276, 31),
        "Beauty": (4000, 0.512, 49),
        "Sports": (3500, 0.405, 91),
        "Toys": (2500, 0.447, 38),
    }
    rows = []
    for offset in range(41, -1, -1):
        day = DEMO_TODAY - timedelta(days=offset)
        trend = 0.94 + ((41 - offset) * 0.0027)
        weekend = 1.16 if day.weekday() in (5, 6) else 1.0
        for store, store_weight in stores.items():
            online_factor = 1.08 if store == "Online" and day.weekday() in (0, 1) else 1.0
            for category, (base, margin_rate, avg_ticket) in categories.items():
                noise = 0.91 + rng.random() * 0.18
                special = 1.0
                if offset == 0 and store == "Downtown Flagship" and category == "Electronics":
                    special = 1.76
                if offset == 1 and store == "Airport" and category == "Apparel":
                    special = 0.58
                if offset <= 3 and store == "Online" and category == "Beauty":
                    special *= 1.24
                revenue = round(base * store_weight * trend * weekend * online_factor * noise * special, 2)
                orders = max(1, round(revenue / avg_ticket))
                gross_margin = round(revenue * (margin_rate + rng.uniform(-0.018, 0.018)), 2)
                returns = round(revenue * rng.uniform(0.012, 0.046), 2)
                rows.append((day.isoformat(), store, category, revenue, orders, gross_margin, returns))
    connection.executemany(
        "INSERT INTO sales_daily(sale_date, store, category, revenue, orders, gross_margin, returns) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_products(connection: sqlite3.Connection) -> None:
    products = [
        (1, "ELE-1048", "Pulse Wireless Headphones", "Electronics", 34, 42, 418, 231),
        (2, "ELE-2281", "Arc 11-inch Tablet", "Electronics", 18, 30, 201, 124),
        (3, "APP-7712", "Harbor Linen Shirt", "Apparel", 156, 80, 526, 288),
        (4, "HOM-3044", "Stoneware Dinner Set", "Home", 47, 55, 298, 174),
        (5, "GRO-9920", "Single-Origin Coffee 1kg", "Grocery", 89, 120, 862, 446),
        (6, "BEA-1824", "Botanical Skin Set", "Beauty", 61, 48, 378, 219),
        (7, "SPO-4480", "Velocity Running Shoes", "Sports", 27, 40, 244, 153),
        (8, "TOY-6007", "City Builder Studio", "Toys", 76, 50, 317, 164),
        (9, "ELE-7510", "Home Mesh Router Pro", "Electronics", 22, 35, 189, 117),
        (10, "APP-2209", "Everyday Travel Tote", "Apparel", 132, 70, 421, 242),
    ]
    connection.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", products)


def _seed_support(connection: sqlite3.Connection) -> None:
    subjects = [
        ("Delivery", "Order hasn’t arrived", "My tracking has not moved for three days and I need the item tomorrow.", "Email", "high", "open"),
        ("Returns", "Exchange the wrong size", "The shirt fits small. Can I exchange it at the Riverside store?", "WhatsApp", "normal", "pending"),
        ("Product", "Tablet warranty question", "Does the Arc tablet include accidental damage cover?", "Email", "normal", "open"),
        ("Payment", "Charged twice at checkout", "My card shows two charges for the same online order.", "WhatsApp", "urgent", "open"),
        ("Availability", "Coffee stock at Downtown", "Is the single-origin coffee available for pickup today?", "WhatsApp", "normal", "resolved"),
        ("Account", "Cannot reset password", "The password reset email never reaches my inbox.", "Email", "high", "pending"),
        ("Delivery", "Change delivery address", "Please redirect my parcel before it leaves the warehouse.", "Email", "high", "open"),
        ("Returns", "Refund timeline", "I returned my headphones last week. When will the refund appear?", "WhatsApp", "normal", "resolved"),
        ("Product", "Dinner set dimensions", "What are the dimensions and total package weight?", "Email", "low", "open"),
        ("Payment", "Promo code rejected", "The weekend code is shown on the banner but fails in checkout.", "WhatsApp", "normal", "pending"),
        ("Availability", "Running shoes size 42", "Can you reserve size 42 at Westgate this evening?", "WhatsApp", "normal", "resolved"),
        ("Account", "Merge loyalty profiles", "I accidentally have two accounts under different emails.", "Email", "low", "open"),
    ]
    customers = [f"Case contact {1041 + index}" for index in range(1, 13)]
    rows = []
    for index, item in enumerate(subjects, start=1):
        category, subject, body, channel, priority, status = item
        created = datetime(2026, 8, 2, 9, 38) - timedelta(minutes=index * 19)
        confidence = round(0.83 + (index % 6) * 0.025, 2)
        response = None if status == "open" else 7 + (index * 3) % 28
        rows.append((index, customers[index - 1], subject, body, channel, category, confidence, priority, status, created.isoformat() + "Z", response))
    connection.executemany("INSERT INTO support_tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _seed_workflow_runs(connection: sqlite3.Connection) -> None:
    now = datetime(2026, 8, 2, 9, 44)
    run_id = 0
    for workflow_id in range(1, 5):
        steps = connection.execute("SELECT id FROM workflow_steps WHERE workflow_id=? ORDER BY position", (workflow_id,)).fetchall()
        for index in range(5):
            run_id += 1
            start = now - timedelta(minutes=(workflow_id * 7 + index * 31))
            is_failed = workflow_id == 4 and index == 2
            status = "failed" if is_failed else "success"
            duration = 6240 if workflow_id == 4 else 720 + workflow_id * 350 + index * 29
            cursor = connection.execute(
                "INSERT INTO workflow_runs(workflow_id, run_key, status, trigger_type, started_at, finished_at, duration_ms, records_processed, error, retries) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (workflow_id, f"seed-{workflow_id}-{index}", status, "schedule" if workflow_id > 2 else "event", start.isoformat() + "Z", (start + timedelta(milliseconds=duration)).isoformat() + "Z", duration, 120 + index * 37, "NetSuite journal period temporarily locked" if is_failed else None, 3 if is_failed else 0),
            )
            saved_run_id = cursor.lastrowid
            for step_index, step in enumerate(steps):
                step_failed = is_failed and step_index == 1
                step_status = "failed" if step_failed else ("skipped" if is_failed and step_index > 1 else "success")
                connection.execute(
                    "INSERT INTO workflow_run_steps(run_id, step_id, status, started_at, duration_ms, output_json, error, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (saved_run_id, step["id"], step_status, (start + timedelta(milliseconds=step_index * 420)).isoformat() + "Z", 0 if step_status == "skipped" else 280 + step_index * 110, json.dumps({"records": 0 if step_status != "success" else 120 + index * 37}), "Ledger lock persisted after retry" if step_failed else None, 3 if step_failed else 1),
                )


def _seed_alerts(connection: sqlite3.Connection) -> None:
    alerts = [
        ("sales-downtown-electronics", "Electronics revenue spike", "Downtown Flagship is 67% above its 14-day baseline. Validate promotion attribution and stock cover.", "high", "AI anomaly detection", "open", "2026-08-02T09:31:00Z", "2026-08-02T10:01:00Z", None),
        ("connector-netsuite-latency", "NetSuite sync latency", "Accounting connector latency reached 1.84s, above the 1.2s warning threshold.", "medium", "Connector monitor", "investigating", "2026-08-02T09:19:00Z", "2026-08-02T10:19:00Z", None),
        ("stock-ele-2281", "Arc Tablet stock risk", "18 units on hand; 14-day forecast is 124. Replenishment is recommended within 24 hours.", "critical", "Demand forecast", "open", "2026-08-02T08:52:00Z", "2026-08-02T09:07:00Z", None),
        ("workflow-finance-close", "Finance close recovered", "A transient ledger lock cleared after three retries; the prior run remains in the audit trail.", "low", "Workflow engine", "acknowledged", "2026-08-02T07:46:00Z", None, "2026-08-02T08:02:00Z"),
        ("support-payment-duplicate", "Urgent payment case", "A duplicate-charge message is awaiting an agent response and is approaching the 15-minute SLA.", "high", "Support triage", "open", "2026-08-02T09:36:00Z", "2026-08-02T09:51:00Z", None),
    ]
    for alert in alerts:
        cursor = connection.execute(
            "INSERT INTO alerts(dedupe_key, title, message, severity, source, status, created_at, escalates_at, acknowledged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", alert
        )
        alert_id = cursor.lastrowid
        severity = alert[3]
        channels = [("In-app", "Ops Command Center")]
        if severity in ("critical", "high"):
            channels.append(("WhatsApp", "Duty manager group"))
        if severity == "critical":
            channels.append(("Email", "Retail operations director"))
        for delivery_index, (channel, recipient) in enumerate(channels, start=1):
            connection.execute(
                "INSERT INTO alert_deliveries(alert_id, channel, recipient, status, attempt, sent_at, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (alert_id, channel, recipient, "delivered", 1, alert[6], f"Accepted by deterministic {channel.lower()} demo adapter"),
            )


def _seed_report_schedules(connection: sqlite3.Connection) -> None:
    schedules = [
        (1, "Daily trading brief", "daily", "Every day · 07:00", "2026-08-03T07:00:00Z", "HTML + CSV", "Store leaders · Finance", 1, "2026-08-02T07:00:12Z"),
        (2, "Weekly operations review", "weekly", "Monday · 06:30", "2026-08-03T06:30:00Z", "HTML + CSV", "Executive team", 1, "2026-07-27T06:30:18Z"),
        (3, "Inventory exception pack", "inventory", "Weekdays · 08:00", "2026-08-03T08:00:00Z", "CSV", "Merchandising", 1, "2026-08-01T08:00:09Z"),
    ]
    connection.executemany("INSERT INTO report_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", schedules)


def _seed_audit(connection: sqlite3.Connection) -> None:
    events = [
        ("workflow", "Order sync completed", "248 records normalized and posted to the KPI layer.", "Workflow engine", "2026-08-02T09:43:42Z"),
        ("connector", "NetSuite moved to degraded", "Latency exceeded the warning threshold for three checks.", "Health monitor", "2026-08-02T09:38:02Z"),
        ("ai", "Demand forecast refreshed", "14-day projections recalculated for 10 priority SKUs.", "Offline AI adapter", "2026-08-02T09:30:00Z"),
        ("report", "Daily trading brief delivered", "HTML and CSV artifacts generated for six stores.", "Report scheduler", "2026-08-02T07:00:12Z"),
        ("alert", "Critical stock alert escalated", "WhatsApp and email delivery receipts recorded.", "Alert engine", "2026-08-02T09:07:03Z"),
    ]
    connection.executemany("INSERT INTO audit_events(event_type, title, detail, actor, created_at) VALUES (?, ?, ?, ?, ?)", events)


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
