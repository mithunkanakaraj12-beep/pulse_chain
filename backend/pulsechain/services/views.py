"""Read-model builders: assemble the JSON shapes the UI consumes from Platform data."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .. import db
from ..domain import (
    CLINIC_BY_ID, DATA_LABEL, DISEASES, FORECAST_MAX_WEEKS, MEDICINE_BY_ID, MEDICINES, REGIONS,
    TREATMENT_PROTOCOL, medicines_for_disease,
)
from . import inventory as inv
from . import redistribution as redis_mod
from . import scenario as scen
from .platform import HISTORY_WEEKS_SHOWN, DISEASE_NAME, NotFoundError, Platform, ValidationError

RISKS = ["critical", "high", "moderate", "low"]


def _sum_lists(lists: list[list[float]]) -> list[float]:
    return [round(sum(vals), 2) for vals in zip(*lists)] if lists else []


def _scope(pl: Platform, conn, region: str | None, clinic_id: str | None) -> list[dict]:
    clinics = pl.clinics(conn)
    if clinic_id:
        if clinic_id not in CLINIC_BY_ID:
            raise NotFoundError("unknown clinic")
        clinics = [c for c in clinics if c["id"] == clinic_id]
    elif region and region != "all":
        if region not in REGIONS:
            raise ValidationError(f"unknown region {region!r}")
        clinics = [c for c in clinics if c["region"] == region]
    return clinics


def _history(conn, as_of: date, clinic_ids: list[str], disease: str, weeks: int = HISTORY_WEEKS_SHOWN) -> list[dict]:
    start = (as_of - timedelta(weeks=weeks - 1)).isoformat()
    ph = ",".join("?" * len(clinic_ids))
    cases = db.rows(conn, f"SELECT week_start, SUM(cases) AS cases FROM cases WHERE disease = ? AND week_start >= ? AND clinic_id IN ({ph}) GROUP BY week_start ORDER BY week_start",
                    (disease, start, *clinic_ids))
    wx = {r["week_start"]: r for r in db.rows(
        conn, f"SELECT week_start, AVG(rain_mm) AS rain_mm, AVG(temp_c) AS temp_c FROM weather WHERE week_start >= ? AND clinic_id IN ({ph}) GROUP BY week_start",
        (start, *clinic_ids))}
    return [{"week_start": r["week_start"], "cases": int(r["cases"]), "rain_mm": round(wx[r["week_start"]]["rain_mm"], 1),
             "temp_c": round(wx[r["week_start"]]["temp_c"], 1)} for r in cases]


def _metrics_summary(pl: Platform, disease: str) -> dict | None:
    m = pl.forecaster.metrics.get("diseases", {}).get(disease)
    if not m:
        return None
    return {
        "test_window": m["test_window"], "model_mae": round(m["model"]["mae"], 2),
        "baseline_mae": {k: round(v["mae"], 2) for k, v in m["baselines"].items()},
        "best_baseline": m["best_baseline"],
        "mae_improvement_vs_best_baseline_pct": round(m["mae_improvement_vs_best_baseline_pct"], 1),
        "heavy_rain_subset": ({k: (round(v, 2) if isinstance(v, float) else v) for k, v in m["heavy_rain_subset"].items()}
                              if m.get("heavy_rain_subset") else None),
        "interval": {"nominal": m["interval"]["nominal_coverage"],
                     "empirical": round(m["interval"]["empirical_coverage_after_cqr"], 3)},
        "by_horizon": [{"horizon": int(h), "model_mae": round(v["model_mae"], 2), "persistence_mae": round(v["persistence_mae"], 2),
                        "climatology_mae": round(v["climatology_mae"], 2)} for h, v in m["by_horizon"].items()],
    }


def forecast_view(pl: Platform, disease: str, region: str | None = "all", weeks: int = 8, clinic_id: str | None = None,
                  scenario: dict | None = None) -> dict:
    if disease not in {d.id for d in DISEASES}:
        raise ValidationError(f"unknown disease {disease!r}")
    if not 1 <= weeks <= FORECAST_MAX_WEEKS:
        raise ValidationError(f"weeks must be between 1 and {FORECAST_MAX_WEEKS}")
    with pl.session() as conn:
        as_of = pl.as_of(conn)
        scenario = scenario if scenario is not None else pl.get_scenario(conn)
        clinics = _scope(pl, conn, region, clinic_id)
        ids = [c["id"] for c in clinics]
        forecasts = pl.forecast_all(conn, scenario)
        history = _history(conn, as_of, ids, disease)
    fcs = [forecasts[(cid, disease)] for cid in ids]
    keys = ("expected", "lower", "upper", "baseline_persistence", "baseline_seasonal")
    series = {k: _sum_lists([[w[k] for w in f["weeks"][:weeks]] for f in fcs]) for k in keys}
    dates = [w["week_start"] for w in fcs[0]["weeks"][:weeks]]
    drivers: dict[str, float] = defaultdict(float)
    typical = []
    for f in fcs:
        for g, v in f["drivers"]["avg_weekly_cases_vs_typical"].items():
            drivers[g] += v
        typical.append(f["drivers"]["expected_if_typical_conditions"][:weeks])
    typical_sum = _sum_lists(typical)
    recent4 = sum(h["cases"] for h in history[-4:]) / 4.0
    exp_total, seas_total = sum(series["expected"]), sum(series["baseline_seasonal"])

    demand = []
    for mid in medicines_for_disease(disease):
        rate = TREATMENT_PROTOCOL[mid][disease]
        exp = [round(v * rate, 1) for v in series["expected"]]
        lo = [round(v * rate, 1) for v in series["lower"]]
        hi = [round(v * rate, 1) for v in series["upper"]]
        norm = [round(v * rate, 1) for v in series["baseline_seasonal"]]
        m = MEDICINE_BY_ID[mid]
        demand.append({"medicine_id": mid, "name": m.name, "short_name": m.short_name, "unit": m.unit,
                       "units_per_case": rate, "weekly_expected": exp, "weekly_lower": lo, "weekly_upper": hi,
                       "weekly_seasonal_norm": norm, "total_expected": round(sum(exp)), "total_seasonal_norm": round(sum(norm)),
                       "uplift_vs_norm_pct": round(100.0 * (sum(exp) / max(sum(norm), 1e-9) - 1), 1)})
    snap = pl.snapshot(scenario)
    meds = set(medicines_for_disease(disease))
    at_risk = [pl._pair_row(p) for p in snap["pairs"] if p["clinic_id"] in ids and p["medicine_id"] in meds
               and inv.RISK_ORDER[p["analysis"]["risk"]] >= inv.RISK_ORDER["moderate"]]
    at_risk.sort(key=lambda r: (r["days_to_stockout"] if r["days_to_stockout"] is not None else 1e9))
    return {
        "disease": disease, "disease_name": DISEASE_NAME[disease], "region": region or "all", "clinic_id": clinic_id,
        "clinic_count": len(ids), "weeks": weeks, "as_of": as_of.isoformat(), "scenario": scenario,
        "history": history, "forecast_dates": dates, "forecast": series,
        "summary": {
            "recent_4wk_avg_cases": round(recent4, 1),
            "forecast_avg_weekly_cases": round(exp_total / weeks, 1),
            "seasonal_norm_avg_weekly_cases": round(seas_total / weeks, 1),
            "change_vs_recent_pct": round(100.0 * (exp_total / weeks / max(recent4, 0.5) - 1), 1),
            "change_vs_seasonal_norm_pct": round(100.0 * (exp_total / max(seas_total, 0.5) - 1), 1),
            "peak_week": dates[series["expected"].index(max(series["expected"]))],
            "peak_expected_cases": max(series["expected"]),
        },
        "drivers": {
            "method": fcs[0]["drivers"]["method"],
            "avg_weekly_cases_vs_typical": {g: round(v, 1) for g, v in drivers.items()},
            "expected_if_typical_conditions": typical_sum,
            "global_importance_pct": fcs[0]["drivers"].get("global_importance_pct", {}),
        },
        "demand": demand, "clinics_at_risk": at_risk,
        "model_evaluation": _metrics_summary(pl, disease),
        "uncertainty_note": "Band = 80% prediction interval, summed across clinics (conservative: real network uncertainty is somewhat narrower).",
        "data_label": DATA_LABEL,
    }


def clinics_list(pl: Platform, scenario: dict | None = None) -> list[dict]:
    snap = pl.snapshot(scenario)
    out = []
    surge_by_clinic: dict[str, list] = defaultdict(list)
    for s in snap["surges"]:
        surge_by_clinic[s["clinic_id"]].append(s)
    for c in {p["clinic_id"]: p["clinic"] for p in snap["pairs"]}.values():
        rows = [p for p in snap["pairs"] if p["clinic_id"] == c["id"]]
        worst = max((p["analysis"]["risk"] for p in rows), key=lambda r: inv.RISK_ORDER[r])
        dts = [p["analysis"]["days_to_stockout"] for p in rows if p["analysis"]["days_to_stockout"] is not None]
        at_risk = [p for p in rows if inv.RISK_ORDER[p["analysis"]["risk"]] >= inv.RISK_ORDER["high"]]
        out.append({
            **{k: c[k] for k in ("id", "name", "region", "region_name", "lat", "lon", "catchment_pop", "lead_time_days", "health_worker", "phone")},
            "worst_risk": worst, "medicines_at_risk": len(at_risk), "medicines_total": len(rows),
            "earliest_stockout_days": min(dts) if dts else None,
            "surge_diseases": [s["disease"] for s in surge_by_clinic[c["id"]] if s["signal"]["is_surge"]],
            "usable_units": sum(p["analysis"]["stock_usable"] for p in rows),
        })
    return out


def clinic_detail(pl: Platform, clinic_id: str, scenario: dict | None = None) -> dict:
    if clinic_id not in CLINIC_BY_ID:
        raise NotFoundError("unknown clinic")
    snap = pl.snapshot(scenario)
    with pl.session() as conn:
        clinic = next(c for c in pl.clinics(conn) if c["id"] == clinic_id)
        as_of = pl.as_of(conn)
        forecasts = pl.forecast_all(conn, snap["scenario"])
        trends = []
        for d in DISEASES:
            fc = forecasts[(clinic_id, d.id)]
            sig = next(s["signal"] for s in snap["surges"] if s["clinic_id"] == clinic_id and s["disease"] == d.id)
            trends.append({"disease": d.id, "disease_name": d.name, "history": _history(conn, as_of, [clinic_id], d.id),
                           "forecast": fc["weeks"], "drivers": fc["drivers"], "signal": sig})
    inventory = pl.inventory_rows(clinic_id=clinic_id, scenario=snap["scenario"])
    nearby = []
    risk_by_clinic: dict[str, str] = {}
    surplus_by_clinic: dict[str, int] = defaultdict(int)
    for p in snap["pairs"]:
        cur = risk_by_clinic.get(p["clinic_id"], "low")
        if inv.RISK_ORDER[p["analysis"]["risk"]] > inv.RISK_ORDER[cur]:
            risk_by_clinic[p["clinic_id"]] = p["analysis"]["risk"]
        surplus_by_clinic[p["clinic_id"]] += p["analysis"]["surplus_units"]
    for other in CLINIC_BY_ID.values():
        if other.id == clinic_id:
            continue
        dist = redis_mod.haversine_km(clinic["lat"], clinic["lon"], other.lat, other.lon) * redis_mod.CIRCUITY_FACTOR
        nearby.append({"id": other.id, "name": other.name, "region": other.region, "distance_km": round(dist, 1),
                       "travel_hours": round(dist / max(1.0, other.road_speed_kmh), 1),
                       "worst_risk": risk_by_clinic.get(other.id, "low"), "surplus_units": surplus_by_clinic[other.id]})
    nearby.sort(key=lambda n: n["distance_km"])
    alerts = [a for a in snap["alerts"] if a["clinic_id"] == clinic_id]
    transfers = [t for t in snap["transfers"] if clinic_id in (t["donor_id"], t["receiver_id"])]
    actions = [{"medicine": r["short_name"], "action": f"Order {r['reorder_qty']} {r['unit']}" + (" now (overdue)" if r["reorder_overdue"] else f" by {r['reorder_by']}"),
                "risk": r["risk"], "days_to_stockout": r["days_to_stockout"]} for r in inventory if r["reorder_qty"] > 0 and r["risk"] != "low"]
    actions.sort(key=lambda a: (a["days_to_stockout"] if a["days_to_stockout"] is not None else 1e9))
    return {"clinic": clinic, "inventory": inventory, "trends": trends, "nearby": nearby[:5], "alerts": alerts,
            "transfers": transfers, "recommended_actions": actions, "scenario": snap["scenario"]}


def dashboard(pl: Platform) -> dict:
    snap = pl.snapshot()
    pairs, today = snap["pairs"], snap["today"]
    with pl.session() as conn:
        as_of = pl.as_of(conn)
        clinics = pl.clinics(conn)
        forecasts = pl.forecast_all(conn, snap["scenario"])
        all_ids = [c["id"] for c in clinics]
        since = (as_of - timedelta(weeks=11)).isoformat()
        cons_hist = defaultdict(dict)
        for r in db.rows(conn, "SELECT medicine_id, week_start, SUM(units) AS u FROM consumption WHERE week_start >= ? GROUP BY 1, 2 ORDER BY 2", (since,)):
            cons_hist[r["medicine_id"]][r["week_start"]] = int(r["u"])
        disease_hist = {d.id: _history(conn, as_of, all_ids, d.id) for d in DISEASES}

    worst: dict[str, str] = {}
    for p in pairs:
        cur = worst.get(p["clinic_id"], "low")
        if inv.RISK_ORDER[p["analysis"]["risk"]] > inv.RISK_ORDER[cur]:
            worst[p["clinic_id"]] = p["analysis"]["risk"]
        worst.setdefault(p["clinic_id"], p["analysis"]["risk"])
    clinics_at_risk = sum(1 for c in clinics if inv.RISK_ORDER[worst.get(c["id"], "low")] >= inv.RISK_ORDER["high"])
    pair_risk = {r: sum(1 for p in pairs if p["analysis"]["risk"] == r) for r in RISKS}
    clinic_risk = {r: sum(1 for c in clinics if worst.get(c["id"], "low") == r) for r in RISKS}

    disease_activity = []
    for d in DISEASES:
        fcs = [forecasts[(cid, d.id)]["weeks"] for cid in all_ids]
        exp = _sum_lists([[w["expected"] for w in f] for f in fcs])
        lo = _sum_lists([[w["lower"] for w in f] for f in fcs])
        hi = _sum_lists([[w["upper"] for w in f] for f in fcs])
        seas = _sum_lists([[w["baseline_seasonal"] for w in f] for f in fcs])
        hist = disease_hist[d.id]
        recent4 = sum(h["cases"] for h in hist[-4:]) / 4.0
        disease_activity.append({
            "disease": d.id, "name": d.name, "history": hist,
            "forecast_dates": [w["week_start"] for w in fcs[0]], "expected": exp, "lower": lo, "upper": hi, "seasonal_norm": seas,
            "next4_avg": round(sum(exp[:4]) / 4, 1), "recent4_avg": round(recent4, 1),
            "vs_seasonal_pct": round(100 * (sum(exp[:4]) / max(sum(seas[:4]), 0.5) - 1), 1)})

    medicine_demand = {}
    for m in MEDICINES:
        mp = [p for p in pairs if p["medicine_id"] == m.id]
        exp = _sum_lists([p["analysis"]["weekly_demand_expected"] for p in mp])
        lo = _sum_lists([p["analysis"]["weekly_demand_lower"] for p in mp])
        hi = _sum_lists([p["analysis"]["weekly_demand_upper"] for p in mp])
        hist = cons_hist[m.id]
        medicine_demand[m.id] = {
            "name": m.name, "short_name": m.short_name, "unit": m.unit,
            "history": [{"week_start": k, "units": v} for k, v in sorted(hist.items())],
            "forecast_dates": [(as_of + timedelta(weeks=h)).isoformat() for h in range(1, FORECAST_MAX_WEEKS + 1)],
            "expected": exp, "lower": lo, "upper": hi}

    inventory_levels = []
    for m in MEDICINES:
        mp = [p for p in pairs if p["medicine_id"] == m.id]
        inventory_levels.append({
            "medicine_id": m.id, "short_name": m.short_name, "unit": m.unit,
            "usable_stock": sum(p["analysis"]["stock_usable"] for p in mp),
            "demand_next_4_weeks": round(sum(sum(p["analysis"]["weekly_demand_expected"][:4]) for p in mp)),
            "demand_next_8_weeks": round(sum(sum(p["analysis"]["weekly_demand_expected"]) for p in mp)),
            "clinics_at_risk": sum(1 for p in mp if inv.RISK_ORDER[p["analysis"]["risk"]] >= inv.RISK_ORDER["high"])})

    regional = []
    for rid, rname in REGIONS.items():
        ids = [c["id"] for c in clinics if c["region"] == rid]
        row = {"region": rid, "name": rname, "clinics": len(ids), "diseases": {}}
        for d in DISEASES:
            exp4 = sum(sum(w["expected"] for w in forecasts[(cid, d.id)]["weeks"][:4]) for cid in ids) / 4
            seas4 = sum(sum(w["baseline_seasonal"] for w in forecasts[(cid, d.id)]["weeks"][:4]) for cid in ids) / 4
            row["diseases"][d.id] = {"expected": round(exp4, 1), "seasonal_norm": round(seas4, 1)}
        regional.append(row)

    replen = [p for p in pairs if p["analysis"]["reorder_qty"] > 0 and p["analysis"]["reorder_by"]
              and date.fromisoformat(p["analysis"]["reorder_by"]) <= today + timedelta(days=21)]
    replen_rows = sorted((pl._pair_row(p) for p in replen), key=lambda r: r["reorder_by"])
    active = [a for a in snap["alerts"] if not a["acknowledged"]]
    return {
        "as_of": as_of.isoformat(), "today": today.isoformat(), "scenario": snap["scenario"], "data_label": DATA_LABEL,
        "kpis": {
            "clinics_monitored": len(clinics),
            "total_usable_units": sum(p["analysis"]["stock_usable"] for p in pairs),
            "expired_units_on_shelf": sum(p["analysis"]["stock_expired"] for p in pairs),
            "clinics_at_risk": clinics_at_risk,
            "pairs_critical": pair_risk["critical"], "pairs_high": pair_risk["high"],
            "active_alerts": len(active),
            "active_alerts_critical": sum(1 for a in active if a["type"] == "critical_shortage"),
            "redistribution_opportunities": len(snap["transfers"]),
            "redistribution_units": sum(t["quantity"] for t in snap["transfers"]),
            "replenishments_due_21d": len(replen_rows),
            "replenishment_units_due_21d": sum(r["reorder_qty"] for r in replen_rows),
            "network_malaria_next4_avg": next(d["next4_avg"] for d in disease_activity if d["disease"] == "malaria"),
            "network_malaria_vs_seasonal_pct": next(d["vs_seasonal_pct"] for d in disease_activity if d["disease"] == "malaria"),
        },
        "disease_activity": disease_activity, "medicine_demand": medicine_demand, "inventory_levels": inventory_levels,
        "risk_distribution": {"by_clinic_medicine": pair_risk, "by_clinic": clinic_risk},
        "regional_activity": regional,
        "top_alerts": active[:6],
        "replenishments": replen_rows[:8],
        "clinic_points": pl._clinic_points(snap),
    }


# --------------------------------------------------------------------------- monsoon walkthrough
def _combined_after_transfers(pairs: list[dict], transfers: list[dict], today: date) -> dict[tuple[str, str], dict]:
    """Receiver stockout projection after ALL proposed transfers into that (clinic, medicine) are applied."""
    incoming: dict[tuple[str, str], list[inv.Batch]] = defaultdict(list)
    for t in transfers:
        for b in t["batches"]:
            incoming[(t["receiver_id"], t["medicine_id"])].append(
                inv.Batch(f"in-{b['batch_id']}", b["quantity"], date.fromisoformat(b["expiry_date"])))
    out = {}
    by_key = {(p["clinic_id"], p["medicine_id"]): p for p in pairs}
    for key, extra in incoming.items():
        p = by_key[key]
        sim = inv.simulate([*p["analysis"]["_batches"], *extra], p["analysis"]["_daily_central"], today)
        dts = sim["stockout_day"]
        out[key] = {"days_to_stockout": None if dts is None else round(dts, 1),
                    "risk": inv.risk_level(dts, p["lead_time_days"]), "received_units": sum(b.quantity for b in extra)}
    return out


def walkthrough(pl: Platform, intensity: float = 1.0, regions: list[str] | None = None, weeks: int = 3) -> dict:
    """Side-by-side baseline vs simulated-monsoon results for the eight demo steps. Read-only: it never
    changes stored state (the UI applies the scenario separately via POST /api/scenario)."""
    monsoon = scen.validate({"monsoon": True, "intensity": intensity, "regions": regions or list(REGIONS), "weeks": weeks})
    baseline = {**monsoon, "monsoon": False}
    before, after = pl.snapshot(baseline), pl.snapshot(monsoon)
    today = after["today"]
    with pl.session() as conn:
        as_of = pl.as_of(conn)
        frames_b = pl._recent_frames(conn, as_of, baseline)
        frames_m = pl._recent_frames(conn, as_of, monsoon)
        clinics = pl.clinics(conn)

    rain = []
    for rid, rname in REGIONS.items():
        ids = [c["id"] for c in clinics if c["region"] == rid]

        def avg_rain(frames):
            vals = [frames["malaria"][cid]["rain_mm"].tail(monsoon["weeks"]).mean() for cid in ids]
            return round(float(sum(vals) / len(vals)), 1)
        rain.append({"region": rid, "name": rname, "affected": rid in monsoon["regions"],
                     "normal_weekly_rain_mm": avg_rain(frames_b), "simulated_weekly_rain_mm": avg_rain(frames_m)})

    fv = {}
    for d in ("malaria", "cholera"):
        b = forecast_view(pl, d, "all", 8, None, baseline)
        m = forecast_view(pl, d, "all", 8, None, monsoon)
        be, me = b["forecast"]["expected"], m["forecast"]["expected"]
        fv[d] = {
            "history": m["history"], "dates": m["forecast_dates"],
            "baseline": b["forecast"], "monsoon": m["forecast"],
            "total_baseline": round(sum(be), 1), "total_monsoon": round(sum(me), 1),
            "increase_pct": round(100 * (sum(me) / max(sum(be), 1e-9) - 1), 1),
            "peak_ratio_vs_baseline": round(max(x / max(y, 0.5) for x, y in zip(me, be)), 2),
            "peak_week": m["summary"]["peak_week"],
            "drivers": m["drivers"]["avg_weekly_cases_vs_typical"],
        }

    impact = []
    for med in MEDICINES:
        pb = [p for p in before["pairs"] if p["medicine_id"] == med.id]
        pm = [p for p in after["pairs"] if p["medicine_id"] == med.id]
        db_ = sum(sum(p["analysis"]["weekly_demand_expected"]) for p in pb)
        dm = sum(sum(p["analysis"]["weekly_demand_expected"]) for p in pm)
        impact.append({"medicine_id": med.id, "short_name": med.short_name, "unit": med.unit,
                       "weekly_before": _sum_lists([p["analysis"]["weekly_demand_expected"] for p in pb]),
                       "weekly_after": _sum_lists([p["analysis"]["weekly_demand_expected"] for p in pm]),
                       "demand_8wk_before": round(db_), "demand_8wk_after": round(dm),
                       "increase_pct": round(100 * (dm / max(db_, 1e-9) - 1), 1)})

    b_by = {(p["clinic_id"], p["medicine_id"]): p["analysis"] for p in before["pairs"]}
    shortages = []
    for p in after["pairs"]:
        a, b = p["analysis"], b_by[(p["clinic_id"], p["medicine_id"])]
        if inv.RISK_ORDER[a["risk"]] >= inv.RISK_ORDER["high"]:
            shortages.append({
                "clinic_id": p["clinic_id"], "clinic_name": p["clinic"]["name"], "medicine_id": p["medicine_id"],
                "short_name": p["medicine"]["short_name"], "unit": p["medicine"]["unit"], "lead_time_days": p["lead_time_days"],
                "stock_usable": a["stock_usable"], "risk_before": b["risk"], "risk_after": a["risk"],
                "days_before": b["days_to_stockout"], "days_after": a["days_to_stockout"],
                "days_after_pessimistic": a["days_to_stockout_pessimistic"], "days_after_optimistic": a["days_to_stockout_optimistic"],
                "naive_days_cover": a["days_cover_at_historical_rate"], "reorder_qty": a["reorder_qty"], "reorder_by": a["reorder_by"]})
    shortages.sort(key=lambda s: s["days_after"] if s["days_after"] is not None else 1e9)

    sev_rank = {"critical": 0, "high": 1, "moderate": 2}
    sms_alerts = sorted(
        (a for a in after["alerts"] if a["sms_eligible"] and a["type"] in ("critical_shortage", "upcoming_shortage")),
        key=lambda a: (sev_rank.get(a["severity"], 3), 0 if a["medicine_id"] in ("act", "rdt") else 1,
                       a["days_to_stockout"] if a["days_to_stockout"] is not None else 1e9))[:6]
    sms_surge = [a for a in after["alerts"] if a["type"] == "disease_surge"][:2]

    combined = _combined_after_transfers(after["pairs"], after["transfers"], today)
    lifted = [{"clinic_id": k[0], "clinic_name": CLINIC_BY_ID[k[0]].name, "medicine_id": k[1], "short_name": MEDICINE_BY_ID[k[1]].short_name,
               "risk_before": next(p["analysis"]["risk"] for p in after["pairs"] if (p["clinic_id"], p["medicine_id"]) == k),
               "risk_after": v["risk"], "days_after": v["days_to_stockout"], "received_units": v["received_units"]} for k, v in combined.items()]

    def n_at_risk(snap):
        return sum(1 for p in snap["pairs"] if inv.RISK_ORDER[p["analysis"]["risk"]] >= inv.RISK_ORDER["high"])
    improved = sum(1 for r in lifted if inv.RISK_ORDER[r["risk_after"]] < inv.RISK_ORDER[r["risk_before"]])
    final = {
        "at_risk_before_monsoon": n_at_risk(before), "at_risk_with_monsoon": n_at_risk(after),
        "at_risk_after_approved_transfers": n_at_risk(after) - sum(
            1 for r in lifted if inv.RISK_ORDER[r["risk_before"]] >= inv.RISK_ORDER["high"] and inv.RISK_ORDER[r["risk_after"]] < inv.RISK_ORDER["high"]),
        "pairs_improved_by_transfers": improved, "units_redistributed": sum(t["quantity"] for t in after["transfers"]),
        "total_reorder_units_recommended": sum(p["analysis"]["reorder_qty"] for p in after["pairs"] if inv.RISK_ORDER[p["analysis"]["risk"]] >= inv.RISK_ORDER["moderate"]),
        "critical_pairs_with_monsoon": sum(1 for p in after["pairs"] if p["analysis"]["risk"] == "critical"),
        "critical_pairs_without_forecast_view": sum(
            1 for p in after["pairs"] if p["analysis"]["days_cover_at_historical_rate"] is not None
            and p["analysis"]["days_cover_at_historical_rate"] <= p["lead_time_days"]),
        "lifted": sorted(lifted, key=lambda r: (inv.RISK_ORDER[r["risk_after"]] - inv.RISK_ORDER[r["risk_before"]], r["clinic_id"])),
    }
    return {
        "scenario": monsoon, "data_label": DATA_LABEL,
        "disclaimer": ("SIMULATED SCENARIO. The monsoon is a weather overlay on synthetic data; the case increase is the model's output on "
                       "that input and is NOT a scientifically established prediction."),
        "steps": {
            "normal": {"risk_distribution": {r: sum(1 for p in before["pairs"] if p["analysis"]["risk"] == r) for r in RISKS},
                       "clinic_points": pl._clinic_points(before), "malaria_next4_avg": round(sum(fv["malaria"]["baseline"]["expected"][:4]) / 4, 1),
                       "malaria_history": fv["malaria"]["history"], "malaria_dates": fv["malaria"]["dates"],
                       "malaria_baseline": fv["malaria"]["baseline"], "transfers_available": len(before["transfers"])},
            "monsoon": {"rain": rain, "weeks": monsoon["weeks"], "intensity": monsoon["intensity"]},
            "forecast": fv,
            "inventory_impact": impact,
            "shortages": {"rows": shortages, "counts_before": {r: sum(1 for p in before["pairs"] if p["analysis"]["risk"] == r) for r in RISKS},
                          "counts_after": {r: sum(1 for p in after["pairs"] if p["analysis"]["risk"] == r) for r in RISKS}},
            "sms": {"messages": [{"alert_key": a["key"], "clinic_name": a["clinic_name"], "health_worker": a["health_worker"],
                                  "phone": a["phone"], "body": a["sms_preview"], "chars": len(a["sms_preview"]), "severity": a["severity"]}
                                 for a in [*sms_alerts, *sms_surge]]},
            "redistribution": {"transfers": after["transfers"], "clinic_points": pl._clinic_points(after)},
            "final": final,
        },
    }
