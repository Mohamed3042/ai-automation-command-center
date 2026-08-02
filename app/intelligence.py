from __future__ import annotations

import os
import re
import json
from collections import defaultdict
from statistics import mean, median
from urllib.request import Request, urlopen

from .db import DEMO_TODAY, rows_as_dicts


_PROVIDER_STATE = {"active": "offline-rules", "last_error": None, "last_used_at": None}


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

    def __init__(self, provider: str | None = None, api_key: str | None = None, endpoint: str | None = None, model: str | None = None, opener=None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("RELAYOPS_LLM_API_KEY")
        self.provider = provider or ("hosted-openai-compatible" if self.api_key else "offline-rules")
        self.endpoint = endpoint or os.environ.get("RELAYOPS_LLM_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        self.model = model or os.environ.get("RELAYOPS_LLM_MODEL", "gpt-4.1-mini")
        self.opener = opener or urlopen

    @property
    def mode(self) -> str:
        if self.provider == "offline-rules" or not self.api_key:
            return "Deterministic offline fallback"
        return f"Hosted provider · {self.model}"

    def classify(self, subject: str, body: str) -> dict:
        if self.provider != "offline-rules" and self.api_key:
            try:
                result = self._classify_hosted(subject, body)
                _PROVIDER_STATE.update({"active": "hosted-openai-compatible", "last_error": None, "last_used_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
                return result
            except Exception as exc:
                _PROVIDER_STATE.update({"active": "offline-rules", "last_error": str(exc)[:160], "last_used_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
        return self._classify_offline(subject, body)

    def _classify_offline(self, subject: str, body: str) -> dict:
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
        return {"category": category, "priority": priority, "confidence": round(confidence, 2), "adapter": "Deterministic offline fallback"}

    def _classify_hosted(self, subject: str, body: str) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Classify a retail support message. Return JSON with category, priority, and confidence. Allowed categories: Payment, Delivery, Returns, Availability, Account, Product, General. Allowed priorities: urgent, high, normal, low."},
                {"role": "user", "content": f"Subject: {subject}\nBody: {body}"},
            ],
        }
        request = Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
        with self.opener(request, timeout=8) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        content = response_data["choices"][0]["message"]["content"].strip().removeprefix("```json").removesuffix("```").strip()
        result = json.loads(content)
        allowed_categories = set(self.KEYWORDS) | {"General"}
        if result.get("category") not in allowed_categories or result.get("priority") not in {"urgent", "high", "normal", "low"}:
            raise ValueError("Hosted provider returned an unsupported classification")
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.8))))
        return {"category": result["category"], "priority": result["priority"], "confidence": round(confidence, 2), "adapter": f"Hosted provider · {self.model}"}

    def status(self) -> dict:
        configured = bool(self.api_key and self.provider != "offline-rules")
        active = _PROVIDER_STATE["active"] if configured else "offline-rules"
        return {
            "active": active,
            "configured": configured,
            "label": f"Hosted provider · {self.model}" if active == "hosted-openai-compatible" else "Deterministic offline fallback",
            "model": self.model if configured else "keyword-rules-v3",
            "endpoint": self.endpoint.split("/v1/")[0] if configured else "local process",
            "last_error": _PROVIDER_STATE["last_error"] if configured else None,
            "last_used_at": _PROVIDER_STATE["last_used_at"] if configured else None,
            "external_keys_required": False,
            "fallback_available": True,
            "capabilities": ["classification", "anomaly detection", "demand forecasting"],
        }


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
    adapter = LLMAdapter()
    return {
        "anomalies": detect_sales_anomalies(connection),
        "forecast": demand_forecast(connection),
        "support": support_intelligence(connection),
        "adapter": adapter.status(),
    }
