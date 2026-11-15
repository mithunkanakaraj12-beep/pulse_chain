"""Alert generation from inventory analyses and disease forecasts (pure functions)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from .inventory import RISK_ORDER
from .sms import compose_shortage_sms, compose_surge_sms

SURGE_RATIO = 1.4        # forecast must exceed the seasonal norm by 40% ...
SURGE_MIN_EXTRA = 3.0    # ... and by at least 3 cases/week on average (ignore noise at tiny counts)
REPLENISH_WINDOW_DAYS = 21


def _cap(text: str) -> str:
    """Upper-case only the first letter (str.capitalize() would turn 'ACT' into 'act')."""
    return text[:1].upper() + text[1:]


def surge_signal(weeks: Sequence[Mapping[str, float]], horizon: int = 4) -> dict[str, Any]:
    """Compare the next `horizon` forecast weeks with the seasonal-climatology baseline."""
    w = list(weeks)[:horizon]
    exp = sum(x["expected"] for x in w) / len(w)
    seasonal = sum(x["baseline_seasonal"] for x in w) / len(w)
    ratio = exp / max(seasonal, 0.5)
    peak = max(w, key=lambda x: x["expected"])
    return {
        "expected_avg": round(exp, 1), "seasonal_avg": round(seasonal, 1), "ratio_vs_seasonal": round(ratio, 2),
        "extra_cases_per_week": round(exp - seasonal, 1), "peak_week": peak["week_start"],
        "is_surge": ratio >= SURGE_RATIO and (exp - seasonal) >= SURGE_MIN_EXTRA,
    }


def build_alerts(*, pairs: Sequence[Mapping[str, Any]], surges: Sequence[Mapping[str, Any]], today: date,
                 acknowledged: set[str], sms_threshold: str, transfer_by_receiver: Mapping[tuple[str, str], str] | None = None) -> list[dict]:
    """Return alerts sorted by severity. `pairs` as in redistribution; `surges` items carry clinic/disease/signal."""
    transfer_by_receiver = transfer_by_receiver or {}
    alerts: list[dict] = []
    sms_rank = RISK_ORDER[sms_threshold]
    for p in pairs:
        a, clinic, med = p["analysis"], p["clinic"], p["medicine"]
        risk = a["risk"]
        rising = (a["forecast_daily_next4w"] or 0) > (a["avg_daily_usage"] or 0) * 1.15
        if RISK_ORDER[risk] >= RISK_ORDER["moderate"]:
            kind = "critical_shortage" if risk == "critical" else "upcoming_shortage"
            key = f"{kind}:{p['clinic_id']}:{p['medicine_id']}"
            days = a["days_to_stockout"]
            sms = compose_shortage_sms(
                clinic_name=clinic["name"], medicine_short=med["short_name"], days_to_stockout=days,
                stockout_date=a["stockout_date"], reorder_qty=a["reorder_qty"], reorder_by=a["reorder_by"],
                risk=risk, demand_rising=rising, overdue=a["reorder_overdue"],
                transfer_from=transfer_by_receiver.get((p["clinic_id"], p["medicine_id"])))
            alerts.append({
                "key": key, "type": kind, "severity": risk,
                "clinic_id": p["clinic_id"], "clinic_name": clinic["name"], "region": clinic["region"],
                "medicine_id": p["medicine_id"], "medicine_name": med["name"], "disease": None,
                "title": f"{_cap(med['short_name'])} - {'critical shortage' if risk == 'critical' else 'shortage ahead'}",
                "message": a["risk_reason"], "days_to_stockout": days, "stockout_date": a["stockout_date"],
                "lead_time_days": p["lead_time_days"],
                "reorder_qty": a["reorder_qty"], "reorder_by": a["reorder_by"],
                "recommendation": (f"Order {a['reorder_qty']} {med['unit']} " + ("immediately" if a["reorder_overdue"] else f"by {a['reorder_by']}")
                                   + "; review nearby surplus for a bridging transfer.") if a["reorder_qty"] else "Review stock levels.",
                "sms_eligible": RISK_ORDER[risk] >= sms_rank, "sms_preview": sms,
                "phone": clinic["phone"], "health_worker": clinic["health_worker"],
                "acknowledged": key in acknowledged,
            })
        elif a["reorder_qty"] > 0 and a["reorder_by"] and date.fromisoformat(a["reorder_by"]) <= today + timedelta(days=REPLENISH_WINDOW_DAYS):
            key = f"replenishment:{p['clinic_id']}:{p['medicine_id']}"
            alerts.append({
                "key": key, "type": "replenishment", "severity": "low",
                "clinic_id": p["clinic_id"], "clinic_name": clinic["name"], "region": clinic["region"],
                "medicine_id": p["medicine_id"], "medicine_name": med["name"], "disease": None,
                "title": f"{_cap(med['short_name'])} - replenishment due",
                "message": f"Order {a['reorder_qty']} {med['unit']} by {a['reorder_by']} to stay covered through the next review period.",
                "days_to_stockout": a["days_to_stockout"], "stockout_date": a["stockout_date"],
                "lead_time_days": p["lead_time_days"],
                "reorder_qty": a["reorder_qty"], "reorder_by": a["reorder_by"],
                "recommendation": f"Place a routine order of {a['reorder_qty']} {med['unit']}.",
                "sms_eligible": False, "sms_preview": None, "phone": clinic["phone"], "health_worker": clinic["health_worker"],
                "acknowledged": key in acknowledged,
            })
    for s in surges:
        sig = s["signal"]
        if not sig["is_surge"]:
            continue
        key = f"disease_surge:{s['clinic_id']}:{s['disease']}"
        sms = compose_surge_sms(clinic_name=s["clinic"]["name"], disease_name=s["disease_name"],
                                ratio_vs_seasonal=sig["ratio_vs_seasonal"], peak_week=sig["peak_week"])
        alerts.append({
            "key": key, "type": "disease_surge", "severity": "high" if sig["ratio_vs_seasonal"] >= 2.0 else "moderate",
            "clinic_id": s["clinic_id"], "clinic_name": s["clinic"]["name"], "region": s["clinic"]["region"],
            "medicine_id": None, "medicine_name": None, "disease": s["disease"],
            "title": f"{s['disease_name']} surge forecast",
            "message": (f"Forecast averages {sig['expected_avg']} cases/week over the next 4 weeks vs a seasonal norm of "
                        f"{sig['seasonal_avg']} ({sig['ratio_vs_seasonal']}x). Peak week of {sig['peak_week']}."),
            "days_to_stockout": None, "stockout_date": None, "lead_time_days": None, "reorder_qty": None, "reorder_by": None,
            "recommendation": "Confirm treatment stock for this disease and brief staff.",
            "sms_eligible": True, "sms_preview": sms, "phone": s["clinic"]["phone"], "health_worker": s["clinic"]["health_worker"],
            "acknowledged": key in acknowledged,
        })
    order = {"critical_shortage": 0, "upcoming_shortage": 1, "disease_surge": 2, "replenishment": 3}
    sev = {"critical": 0, "high": 1, "moderate": 2, "low": 3}
    alerts.sort(key=lambda x: (order[x["type"]], sev[x["severity"]], x["days_to_stockout"] if x["days_to_stockout"] is not None else 1e9))
    return alerts
