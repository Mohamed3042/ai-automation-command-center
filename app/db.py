from __future__ import annotations

import json
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
    connection.execute("PRAGMA busy_timeout = 10000")
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
    failure_policy TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'event',
    schedule_cron TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    connector_slug TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    retry_limit INTEGER NOT NULL DEFAULT 2,
    retry_backoff_ms INTEGER NOT NULL DEFAULT 5,
    manual_minutes REAL NOT NULL DEFAULT 0,
    estimate_basis TEXT NOT NULL DEFAULT 'per_run',
    active INTEGER NOT NULL DEFAULT 1
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
    retries INTEGER NOT NULL DEFAULT 0,
    evidence_source TEXT NOT NULL DEFAULT 'executed'
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
    attempt INTEGER NOT NULL DEFAULT 1,
    records_processed INTEGER NOT NULL DEFAULT 0,
    step_name TEXT,
    manual_minutes REAL NOT NULL DEFAULT 0,
    estimate_basis TEXT NOT NULL DEFAULT 'per_run'
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
    response_minutes INTEGER,
    routed_queue TEXT,
    classified_at TEXT
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
    acknowledged_at TEXT,
    investigating_at TEXT,
    resolved_at TEXT,
    muted_until TEXT,
    escalation_level INTEGER NOT NULL DEFAULT 0
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
    last_run TEXT,
    cron_expr TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC'
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
    size_bytes INTEGER NOT NULL,
    generated_by TEXT NOT NULL DEFAULT 'Legacy report process'
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staged_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE NOT NULL,
    channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    staged_at TEXT NOT NULL,
    validated_at TEXT,
    normalized_at TEXT
);

CREATE TABLE IF NOT EXISTS canonical_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staged_order_id INTEGER UNIQUE NOT NULL REFERENCES staged_orders(id),
    external_id TEXT UNIQUE NOT NULL,
    channel TEXT NOT NULL,
    store TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    order_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_order_id INTEGER NOT NULL REFERENCES canonical_orders(id),
    account TEXT NOT NULL,
    debit REAL NOT NULL DEFAULT 0,
    credit REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(canonical_order_id, account)
);

CREATE TABLE IF NOT EXISTS kpi_cache (
    metric_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    value REAL NOT NULL,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY(metric_key, scope)
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    stock INTEGER NOT NULL,
    forecast_units INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replenishment_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    sku TEXT NOT NULL,
    recommended_qty INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    source_run_id INTEGER REFERENCES workflow_runs(id),
    created_at TEXT NOT NULL,
    UNIQUE(product_id, status)
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    template TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    source_run_id INTEGER REFERENCES workflow_runs(id),
    sent_at TEXT NOT NULL,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS alert_mute_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pattern TEXT NOT NULL DEFAULT '*',
    severity TEXT NOT NULL DEFAULT '*',
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    status TEXT NOT NULL,
    last_tick_at TEXT,
    next_tick_at TEXT,
    jobs_fired INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
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
    (1, "Omnichannel order sync", "Validates staged POS/web orders, writes canonical orders and balanced ledger entries, then refreshes KPI cache rows.", "Webhook · every order", 1, 0, 0, 0, "Commerce Ops", "Retry each step 2× · bounded backoff · escalate", "event", None, None, "2026-08-02T09:00:00Z", "2026-08-02T09:00:00Z"),
    (2, "Support triage & routing", "Reads unclassified conversations, classifies through LLMAdapter, scores priority, and persists queue assignments.", "New email or message", 1, 0, 0, 0, "Customer Care", "Retry each step 2× · dead-letter alert", "event", None, None, "2026-08-02T09:00:00Z", "2026-08-02T09:00:00Z"),
    (3, "Inventory risk monitor", "Snapshots stock, refreshes the forecast, creates replenishment tasks, and notifies the buyer queue.", "Every 30 minutes", 1, 0, 0, 0, "Merchandising", "Retry each step 3× · alert on-call", "cron", "*/30 * * * *", "2026-08-02T10:00:00Z", "2026-08-02T09:00:00Z", "2026-08-02T09:00:00Z"),
    (4, "Daily finance close", "Aggregates channel totals, reconciles real ledger rows, renders files, and records distribution.", "Daily · 23:30", 1, 0, 0, 0, "Finance", "Retry each step 3× · notify controller", "cron", "30 23 * * *", "2026-08-02T23:30:00Z", "2026-08-02T09:00:00Z", "2026-08-02T09:00:00Z"),
]


WORKFLOW_STEPS = [
    (1, 1, 1, "Validate staged orders", "webhook.validate", "shopify", '{"schema":"order.v2"}', 2, 5, 0.10, "per_record"),
    (2, 1, 2, "Normalize orders", "transform.map", None, '{"mapping":"canonical_order"}', 2, 5, 0.35, "per_record"),
    (3, 1, 3, "Post ledger entries", "accounting.post", "netsuite", '{"ledger":"sales"}', 2, 5, 0.60, "per_record"),
    (4, 1, 4, "Refresh KPI cache", "metrics.increment", None, '{"scope":"store,channel"}', 1, 5, 4.0, "per_run"),
    (5, 2, 1, "Read unclassified conversations", "message.receive", "microsoft-mail", '{}', 2, 5, 0.25, "per_record"),
    (6, 2, 2, "Classify intent", "ai.classify", None, '{"fallback":"keyword_rules_v3"}', 2, 5, 1.20, "per_record"),
    (7, 2, 3, "Score priority", "rules.evaluate", None, '{"policy":"support_sla"}', 2, 5, 0.40, "per_record"),
    (8, 2, 4, "Route to queue", "ticket.assign", None, '{"queues":6}', 2, 5, 0.60, "per_record"),
    (9, 3, 1, "Snapshot stock levels", "sheet.read", "google-sheets", '{"range":"Inventory!A:H"}', 3, 5, 0.35, "per_record"),
    (10, 3, 2, "Refresh demand forecast", "ai.forecast", None, '{"horizon_days":14}', 3, 5, 6.0, "per_run"),
    (11, 3, 3, "Create replenishment tasks", "rules.evaluate", None, '{"threshold":"cover_days < 8"}', 3, 5, 1.20, "per_record"),
    (12, 3, 4, "Notify buyer queue", "message.send", "whatsapp", '{"template":"inventory_risk"}', 3, 5, 0.50, "per_record"),
    (13, 4, 1, "Aggregate channel totals", "sales.aggregate", "square-pos", '{"window":"day"}', 3, 5, 12.0, "per_run"),
    (14, 4, 2, "Reconcile ledger", "accounting.reconcile", "netsuite", '{"tolerance":25}', 3, 5, 15.0, "per_run"),
    (15, 4, 3, "Generate daily close report", "report.render", None, '{"type":"daily"}', 2, 5, 8.0, "per_run"),
    (16, 4, 4, "Record report distribution", "email.send", "microsoft-mail", '{"audience":"leadership"}', 2, 5, 2.0, "per_run"),
]


def init_db(path: str | Path | None = None, reset: bool = False) -> sqlite3.Connection:
    db_path = Path(path or os.environ.get("RELAYOPS_DB", DEFAULT_DB_PATH))
    if reset and db_path.exists():
        db_path.unlink()
    connection = get_connection(db_path)
    connection.executescript(SCHEMA)
    _migrate(connection)
    exists = connection.execute("SELECT value FROM metadata WHERE key='seed_version'").fetchone()
    if not exists:
        _seed(connection)
    else:
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('seed_version', '1.1.0')")
        connection.commit()
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Apply additive v1.1 columns to a v1.0 database without data loss."""
    additions = {
        "workflows": [
            ("trigger_type", "TEXT NOT NULL DEFAULT 'event'"), ("schedule_cron", "TEXT"),
            ("next_run_at", "TEXT"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
        ],
        "workflow_steps": [
            ("retry_limit", "INTEGER NOT NULL DEFAULT 2"), ("retry_backoff_ms", "INTEGER NOT NULL DEFAULT 5"),
            ("manual_minutes", "REAL NOT NULL DEFAULT 0"), ("estimate_basis", "TEXT NOT NULL DEFAULT 'per_run'"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ],
        "workflow_run_steps": [
            ("records_processed", "INTEGER NOT NULL DEFAULT 0"), ("step_name", "TEXT"),
            ("manual_minutes", "REAL NOT NULL DEFAULT 0"), ("estimate_basis", "TEXT NOT NULL DEFAULT 'per_run'"),
        ],
        "workflow_runs": [("evidence_source", "TEXT NOT NULL DEFAULT 'executed'")],
        "support_tickets": [("routed_queue", "TEXT"), ("classified_at", "TEXT")],
        "alerts": [
            ("investigating_at", "TEXT"), ("resolved_at", "TEXT"), ("muted_until", "TEXT"),
            ("escalation_level", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "report_schedules": [("cron_expr", "TEXT"), ("timezone", "TEXT NOT NULL DEFAULT 'UTC'")],
        "reports": [("generated_by", "TEXT NOT NULL DEFAULT 'Legacy report process'")],
        "outbound_messages": [("dedupe_key", "TEXT")],
    }
    prior_version_row = connection.execute("SELECT value FROM metadata WHERE key='seed_version'").fetchone()
    prior_version = prior_version_row["value"] if prior_version_row else None
    for table, columns in additions.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    now = "2026-08-02T09:00:00Z"
    connection.execute("UPDATE workflows SET created_at=COALESCE(created_at, ?), updated_at=COALESCE(updated_at, ?)", (now, now))
    connection.execute("UPDATE workflows SET trigger_type='cron', schedule_cron='*/30 * * * *', next_run_at=COALESCE(next_run_at,'2026-08-02T10:00:00Z') WHERE id=3")
    connection.execute("UPDATE workflows SET trigger_type='cron', schedule_cron='30 23 * * *', next_run_at=COALESCE(next_run_at,'2026-08-02T23:30:00Z') WHERE id=4")
    connection.execute(
        "UPDATE workflow_run_steps SET step_name=(SELECT name FROM workflow_steps WHERE workflow_steps.id=workflow_run_steps.step_id),manual_minutes=(SELECT manual_minutes FROM workflow_steps WHERE workflow_steps.id=workflow_run_steps.step_id),estimate_basis=(SELECT estimate_basis FROM workflow_steps WHERE workflow_steps.id=workflow_run_steps.step_id) WHERE step_name IS NULL"
    )
    if prior_version == "1.0.0":
        connection.execute("UPDATE workflow_runs SET evidence_source='legacy_seed'")
        for step in WORKFLOW_STEPS:
            connection.execute(
                "UPDATE workflow_steps SET name=?,action=?,connector_slug=?,config_json=?,retry_limit=?,retry_backoff_ms=?,manual_minutes=?,estimate_basis=? WHERE id=? AND workflow_id=?",
                (step[3], step[4], step[5], step[6], step[7], step[8], step[9], step[10], step[0], step[1]),
            )
        for workflow in WORKFLOWS:
            connection.execute(
                "UPDATE workflows SET description=?,trigger=?,failure_policy=?,trigger_type=?,schedule_cron=?,next_run_at=?,updated_at=? WHERE id=?",
                (workflow[2], workflow[3], workflow[9], workflow[10], workflow[11], workflow[12], now, workflow[0]),
            )
    connection.execute("UPDATE report_schedules SET cron_expr=CASE id WHEN 1 THEN '0 7 * * *' WHEN 2 THEN '30 6 * * 1' ELSE '0 8 * * 1-5' END WHERE cron_expr IS NULL")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_messages_dedupe ON outbound_messages(dedupe_key) WHERE dedupe_key IS NOT NULL")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_workflow_schedule_due ON workflows(active,trigger_type,next_run_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_alert_escalation_due ON alerts(status,escalates_at)")
    connection.execute("INSERT OR IGNORE INTO scheduler_state(id, status, jobs_fired, errors) VALUES (1, 'stopped', 0, 0)")
    connection.commit()


def _seed(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO connectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        CONNECTORS,
    )
    connection.executemany(
        "INSERT INTO workflows(id,name,description,trigger,active,runs_30d,success_rate,avg_duration_ms,owner,failure_policy,trigger_type,schedule_cron,next_run_at,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", WORKFLOWS,
    )
    connection.executemany(
        "INSERT INTO workflow_steps(id,workflow_id,position,name,action,connector_slug,config_json,retry_limit,retry_backoff_ms,manual_minutes,estimate_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", WORKFLOW_STEPS,
    )
    _seed_sales(connection)
    _seed_products(connection)
    _seed_support(connection)
    _seed_staged_orders(connection)
    _seed_alerts(connection)
    _seed_report_schedules(connection)
    _seed_audit(connection)
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("seed_version", "1.1.0"),
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
        is_unclassified = index <= 4
        stored_category = "Unclassified" if is_unclassified else category
        confidence = 0.0 if is_unclassified else round(0.83 + (index % 6) * 0.025, 2)
        stored_priority = "normal" if is_unclassified else priority
        response = None if status == "open" else 7 + (index * 3) % 28
        rows.append((index, customers[index - 1], subject, body, channel, stored_category, confidence, stored_priority, status, created.isoformat() + "Z", response))
    connection.executemany("INSERT INTO support_tickets(id,customer,subject,body,channel,category,confidence,priority,status,created_at,response_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _seed_staged_orders(connection: sqlite3.Connection) -> None:
    channels = ["Shopify", "Square POS"]
    stores = ["Online", "Downtown Flagship", "Riverside", "Westgate"]
    rows = []
    for index in range(1, 13):
        channel = channels[index % 2]
        payload = {
            "order_id": f"DEMO-{2026000 + index}",
            "store": stores[index % len(stores)],
            "amount": round(42.50 + index * 17.35, 2),
            "currency": "USD",
            "status": "paid",
            "created_at": f"2026-08-02T09:{index + 10:02d}:00Z",
        }
        rows.append((payload["order_id"], channel, json.dumps(payload, sort_keys=True), "pending", f"2026-08-02T09:{index + 10:02d}:01Z"))
    connection.executemany("INSERT INTO staged_orders(external_id,channel,payload_json,status,staged_at) VALUES (?, ?, ?, ?, ?)", rows)


def _seed_alerts(connection: sqlite3.Connection) -> None:
    alerts = [
        ("sales-downtown-electronics", "Electronics revenue spike", "Downtown Flagship is 67% above its 14-day baseline. Validate promotion attribution and stock cover.", "high", "AI anomaly detection", "open", "2026-08-02T09:31:00Z", "2026-08-02T10:01:00Z", None, None),
        ("connector-netsuite-latency", "NetSuite sync latency", "Accounting connector latency reached 1.84s, above the 1.2s warning threshold.", "medium", "Connector monitor", "investigating", "2026-08-02T09:19:00Z", "2026-08-02T10:19:00Z", "2026-08-02T09:23:00Z", "2026-08-02T09:26:00Z"),
        ("stock-ele-2281", "Arc Tablet stock risk", "18 units on hand; 14-day forecast is 124. Replenishment is recommended within 24 hours.", "critical", "Demand forecast", "open", "2026-08-02T08:52:00Z", "2026-08-02T09:07:00Z", None, None),
        ("support-payment-duplicate", "Urgent payment case", "A duplicate-charge message is awaiting an agent response and is approaching the 15-minute SLA.", "high", "Support triage", "open", "2026-08-02T09:36:00Z", "2026-08-02T09:51:00Z", None, None),
    ]
    for alert in alerts:
        cursor = connection.execute(
            "INSERT INTO alerts(dedupe_key, title, message, severity, source, status, created_at, escalates_at, acknowledged_at, investigating_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", alert
        )
        alert_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO alert_timeline(alert_id,event_type,from_status,to_status,actor,detail,created_at) VALUES (?, 'created', NULL, ?, 'Rule engine', 'Threshold condition created the alert.', ?)",
            (alert_id, alert[5], alert[6]),
        )
        if alert[5] == "investigating":
            connection.execute(
                "INSERT INTO alert_timeline(alert_id,event_type,from_status,to_status,actor,detail,created_at) VALUES (?, 'transition', 'open', 'acknowledged', 'Connector owner', 'Owner acknowledged the connector exception.', ?)",
                (alert_id, alert[8]),
            )
            connection.execute(
                "INSERT INTO alert_timeline(alert_id,event_type,from_status,to_status,actor,detail,created_at) VALUES (?, 'transition', 'acknowledged', 'investigating', 'Connector owner', 'Owner began investigation.', ?)",
                (alert_id, alert[9]),
            )
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
        (1, "Daily trading brief", "daily", "Every day · 07:00", "2026-08-03T07:00:00Z", "HTML + CSV", "Store leaders · Finance", 1, None, "0 7 * * *", "UTC"),
        (2, "Weekly operations review", "weekly", "Monday · 06:30", "2026-08-03T06:30:00Z", "HTML + CSV", "Executive team", 1, None, "30 6 * * 1", "UTC"),
        (3, "Inventory exception pack", "inventory", "Weekdays · 08:00", "2026-08-03T08:00:00Z", "CSV", "Merchandising", 1, None, "0 8 * * 1-5", "UTC"),
    ]
    connection.executemany("INSERT INTO report_schedules(id,name,report_type,frequency,next_run,format,recipients,active,last_run,cron_expr,timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", schedules)


def _seed_audit(connection: sqlite3.Connection) -> None:
    events = [
        ("workflow", "Order batch staged", "12 deterministic POS and web orders are ready for executable workflow processing.", "Connector adapters", "2026-08-02T09:43:42Z"),
        ("connector", "NetSuite moved to degraded", "Latency exceeded the warning threshold for three checks.", "Health monitor", "2026-08-02T09:38:02Z"),
        ("ai", "Demand forecast refreshed", "14-day projections recalculated for 10 priority SKUs.", "Offline AI adapter", "2026-08-02T09:30:00Z"),
        ("report", "Report schedules loaded", "Daily, weekly, and inventory cron schedules are active.", "Report scheduler", "2026-08-02T07:00:12Z"),
        ("alert", "Critical stock condition detected", "Arc Tablet breached the configured stock-cover threshold.", "Alert engine", "2026-08-02T09:07:03Z"),
    ]
    connection.executemany("INSERT INTO audit_events(event_type, title, detail, actor, created_at) VALUES (?, ?, ?, ?, ?)", events)


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
