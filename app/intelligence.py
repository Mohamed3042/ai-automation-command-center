from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from statistics import mean, median

from .db import DEMO_TODAY, rows_as_dicts


class LLMAdapter:
    """Provider boundary with a deterministic local implementation.

    A production provider can be attached without changing workflow code. The
    portfolio build deliberately defaults to the offline adapter and makes no
    network calls or API-key checks.
    """

    KEYWORDS = {
        "Payment": ("charged", "card", "payment", "checkout", "promo", "refund"),
        "Delivery": ("delivery", "tracking", "parcel", "arrive", "address", "warehouse"),
        "Returns": ("return", "exchange", "wrong size"),
        "Availability": ("available", "stock", "reserve", "pickup", "size"),
        "Account": ("password", "account", "loyalty", "login", "email"),
        "Product": ("warranty", "dimensions", "weight", "include", "cover"),
    }

    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or os.environ.get("RELAYOPS_LLM_PROVIDER", "offline-rules")

    @property
    def mode(self) -> str:
        return "Deterministic offline fallback" if self.provider == "offline-rules" else self.provider

    def classify(self, subject: str, body: str) -> dict:
        text = re.sub(r"[^a-z0-9 ]", " ", f"{subject} {body}".lower())
        scores = {}
        for category, keywords in self.KEYWORDS.items():
            scores[category] = sum(2 if f" {word} " in f" {text} " else 1 if word in text else 0 for word in keywords)
        category, score = max(scores.items(), key=lambda item: (item[1], item[0]))
        if score == 0:
            category = "General"
        urgency_terms = ("urgent", "charged twice", "tomorrow", "not arrived", "never", "fails")
        urgency = sum(term in text for term in urgency_terms)
        priority = "urgent" if urgency >= 2 or "charged twice" in text else "high" if urgency else "normal"
        confidence = min(0.98, 0.76 + score * 0.045)
        return {"category": category, "priority": priority, "confidence": round(confidence, 2), "adapter": self.mode}


def _mad_zscore(value: float, history: list[float]) -> float:
    if not history:
        return 0.0
    baseline = median(history)
    deviations = [abs(item - baseline) for item in history]
    mad = median(deviations) or max(1.0, baseline * 0.025)
    return 0.6745 * (value - baseline) / mad


def detect_sales_anomalies(connection) -> list[dict]:
    rows = connection.execute(
        "SELECT sale_date, store, category, revenue FROM sales_daily WHERE sale_date >= date(?, '-14 days') ORDER BY sale_date",
        (DEMO_TODAY.isoformat(),),
    ).fetchall()
    series: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        series[(row["store"], row["category"])].append((row["sale_date"], row["revenue"]))
    anomalies = []
    for (store, category), values in series.items():
        current_date, current = values[-1]
        history = [amount for _, amount in values[:-1]]
        baseline = mean(history) if history else current
        score = _mad_zscore(current, history)
        change = (current / baseline - 1) * 100 if baseline else 0
        if abs(score) >= 2.8 and abs(change) >= 30:
            anomalies.append({
                "store": store,
                "category": category,
                "date": current_date,
                "actual": round(current, 2),
                "baseline": round(baseline, 2),
                "change_pct": round(change, 1),
                "score": round(score, 2),
                "direction": "spike" if change > 0 else "drop",
                "severity": "high" if abs(change) >= 45 else "medium",
                "model": "Robust MAD · 14-day rolling baseline",
            })
    return sorted(anomalies, key=lambda item: abs(item["change_pct"]), reverse=True)[:8]


def demand_forecast(connection, horizon: int = 14) -> dict:
    rows = connection.execute(
        "SELECT sale_date, SUM(revenue) revenue FROM sales_daily WHERE sale_date >= date(?, '-27 days') GROUP BY sale_date ORDER BY sale_date",
        (DEMO_TODAY.isoformat(),),
    ).fetchall()
    values = [float(row["revenue"]) for row in rows]
    x_values = list(range(len(values)))
    x_mean = mean(x_values)
    y_mean = mean(values)
    denominator = sum((x - x_mean) ** 2 for x in x_values) or 1
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values)) / denominator
    weekday_factors = defaultdict(list)
    for row in rows:
        weekday_factors[__import__("datetime").date.fromisoformat(row["sale_date"]).weekday()].append(float(row["revenue"]) / y_mean)
    points = []
    for step in range(1, horizon + 1):
        forecast_day = DEMO_TODAY + __import__("datetime").timedelta(days=step)
        factor = mean(weekday_factors[forecast_day.weekday()]) if weekday_factors[forecast_day.weekday()] else 1.0
        predicted = (y_mean + slope * (len(values) + step - x_mean)) * factor
        points.append({
            "date": forecast_day.isoformat(),
            "revenue": round(predicted, 2),
            "lower": round(predicted * 0.91, 2),
            "upper": round(predicted * 1.09, 2),
        })
    inventory = rows_as_dicts(connection.execute(
        "SELECT sku, name, category, stock, reorder_point, forecast_units, ROUND(stock * 14.0 / forecast_units, 1) cover_days FROM products WHERE stock < reorder_point OR stock * 14.0 / forecast_units < 8 ORDER BY cover_days"
    ).fetchall())
    return {
        "points": points,
        "horizon_days": horizon,
        "projected_revenue": round(sum(point["revenue"] for point in points), 2),
        "trend_pct": round((points[-1]["revenue"] / points[0]["revenue"] - 1) * 100, 1),
        "confidence": 0.91,
        "model": "Trend + weekday seasonality · deterministic v1",
        "inventory_risks": inventory,
    }


def support_intelligence(connection) -> dict:
    tickets = rows_as_dicts(connection.execute("SELECT * FROM support_tickets ORDER BY created_at DESC").fetchall())
    for ticket in tickets:
        ticket["confidence_pct"] = round(ticket["confidence"] * 100)
    classified = len(tickets)
    auto_routed = sum(ticket["confidence"] >= 0.85 for ticket in tickets)
    responded = [ticket["response_minutes"] for ticket in tickets if ticket["response_minutes"] is not None]
    by_category = connection.execute("SELECT category, COUNT(*) count FROM support_tickets GROUP BY category ORDER BY count DESC, category").fetchall()
    return {
        "tickets": tickets,
        "metrics": {
            "classified": classified,
            "auto_routed": auto_routed,
            "automation_rate": round(auto_routed / classified * 100, 1) if classified else 0,
            "median_first_response_minutes": median(responded) if responded else None,
            "urgent_open": sum(ticket["priority"] == "urgent" and ticket["status"] == "open" for ticket in tickets),
        },
        "categories": rows_as_dicts(by_category),
        "adapter": LLMAdapter().mode,
    }


def intelligence_payload(connection) -> dict:
    return {
        "anomalies": detect_sales_anomalies(connection),
        "forecast": demand_forecast(connection),
        "support": support_intelligence(connection),
        "adapter": {
            "active": "offline-rules",
            "label": "Deterministic offline fallback",
            "external_keys_required": False,
            "capabilities": ["classification", "anomaly detection", "demand forecasting"],
        },
    }
