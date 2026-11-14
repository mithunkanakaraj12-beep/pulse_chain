"""Transparent inventory projection engine (pure functions, no I/O).

Every number is derived by a documented rule from (a) batches on the shelf, (b) the weekly demand
projection coming from the disease forecasts x treatment protocol, and (c) the clinic's resupply lead
time. There is no opaque scoring.

Rules
-----
* Daily demand for day d = weekly projected demand of week (d // 7) / 7. Beyond the forecast horizon the
  mean of the last four forecast weeks is held flat (flagged as extrapolated in the output).
* Stock is consumed first-expiry-first-out. A batch is unusable on and after its expiry date.
* days_to_stockout = first day the day's demand cannot be met (fractional, interpolated within the day).
  It is computed three times: central demand, pessimistic demand (upper 90% bound), optimistic (lower 10%).
* safety_stock = max(policy minimum, upper-bound demand - central demand over the resupply lead time).
* reorder_qty = demand over (lead time + review period) + safety stock - stock that will actually be
  consumed (usable stock minus projected expiry waste), rounded UP to the medicine's order multiple.
* reorder_by = stockout date - lead time (today if already late).
* Risk (relative to the resupply lead time, because a stockout only matters if it arrives before
  replenishment can):  critical: stockout <= lead time | high: <= lead + 7 d | moderate: <= lead + 21 d | low.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

SIM_DAYS = 180
REVIEW_DAYS = 28
RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
RISK_MARGINS_DAYS = {"high": 7, "moderate": 21}


@dataclass(frozen=True)
class Batch:
    batch_id: int | str
    quantity: int
    expiry: date

    def usable_on(self, day: date) -> bool:
        return self.expiry > day


def risk_level(days_to_stockout: float | None, lead_time_days: int) -> str:
    if days_to_stockout is None:
        return "low"
    if days_to_stockout <= lead_time_days:
        return "critical"
    if days_to_stockout <= lead_time_days + RISK_MARGINS_DAYS["high"]:
        return "high"
    if days_to_stockout <= lead_time_days + RISK_MARGINS_DAYS["moderate"]:
        return "moderate"
    return "low"


def daily_series(weekly: Sequence[float], n_days: int = SIM_DAYS) -> tuple[list[float], bool]:
    """Expand weekly demand into daily demand; returns (series, extrapolated_beyond_horizon)."""
    if not weekly:
        return [0.0] * n_days, True
    tail = sum(weekly[-4:]) / len(weekly[-4:])
    out = []
    for d in range(n_days):
        w = d // 7
        out.append((weekly[w] if w < len(weekly) else tail) / 7.0)
    return out, n_days > len(weekly) * 7


def total_usable(batches: Sequence[Batch], day: date) -> int:
    return sum(b.quantity for b in batches if b.usable_on(day))


def simulate(batches: Sequence[Batch], daily: Sequence[float], today: date) -> dict:
    """FEFO day-by-day simulation. Returns stockout_day (float|None), wasted units, remaining at end."""
    stock = sorted(([b.expiry, b.quantity] for b in batches if b.quantity > 0), key=lambda x: x[0])
    wasted = 0.0
    for d, demand in enumerate(daily):
        day = today + timedelta(days=d)
        for entry in stock:
            if entry[1] > 0 and entry[0] <= day:
                wasted += entry[1]
                entry[1] = 0
        available = sum(e[1] for e in stock)
        if demand > 0 and available < demand - 1e-9:
            return {"stockout_day": d + available / demand, "wasted": wasted, "remaining": 0.0}
        need = demand
        for entry in stock:
            if need <= 0:
                break
            take = min(entry[1], need)
            entry[1] -= take
            need -= take
    remaining = sum(e[1] for e in stock)
    return {"stockout_day": None, "wasted": wasted, "remaining": remaining}


def remaining_after(batches: Sequence[Batch], daily: Sequence[float], today: date, n_days: int) -> float:
    """Usable units still on the shelf after `n_days` of consumption (0 if stocked out before that)."""
    stock = sorted(([b.expiry, b.quantity] for b in batches if b.quantity > 0), key=lambda x: x[0])
    for d in range(min(n_days, len(daily))):
        day = today + timedelta(days=d)
        for entry in stock:
            if entry[1] > 0 and entry[0] <= day:
                entry[1] = 0
        need = daily[d]
        for entry in stock:
            if need <= 0:
                break
            take = min(entry[1], need)
            entry[1] -= take
            need -= take
        if need > 1e-9:
            return 0.0
    end_day = today + timedelta(days=n_days)
    return float(sum(e[1] for e in stock if e[0] > end_day))


def demand_over(daily: Sequence[float], days: float) -> float:
    whole = int(math.floor(days))
    total = sum(daily[:whole])
    if whole < len(daily):
        total += daily[whole] * (days - whole)
    return total


def round_up(value: float, multiple: int) -> int:
    if value <= 0:
        return 0
    return int(math.ceil(value / multiple) * multiple)


def analyze_pair(
    *, batches: Sequence[Batch], today: date, lead_time_days: int, min_safety_stock: int,
    order_multiple: int, weekly_expected: Sequence[float], weekly_lower: Sequence[float],
    weekly_upper: Sequence[float], historical_avg_daily: float,
) -> dict:
    """Full projection for one (clinic, medicine) pair. Returns a JSON-friendly dict; the private
    keys `_daily_*` and `_batches` are used by the redistribution engine and stripped before sending."""
    daily_c, extrapolated = daily_series(weekly_expected)
    daily_hi, _ = daily_series(weekly_upper)
    daily_lo, _ = daily_series(weekly_lower)
    daily_hist = [historical_avg_daily] * SIM_DAYS

    expired_units = sum(b.quantity for b in batches if not b.usable_on(today))
    usable = total_usable(batches, today)

    central = simulate(batches, daily_c, today)
    pess = simulate(batches, daily_hi, today)
    opt = simulate(batches, daily_lo, today)
    no_forecast = simulate(batches, daily_hist, today)

    dts = central["stockout_day"]
    lead_demand = demand_over(daily_c, lead_time_days)
    lead_demand_hi = demand_over(daily_hi, lead_time_days)
    safety = max(min_safety_stock, int(math.ceil(max(0.0, lead_demand_hi - lead_demand))))
    horizon_days = lead_time_days + REVIEW_DAYS
    horizon_demand = demand_over(daily_c, horizon_days)
    waste = central["wasted"]
    consumable = max(0.0, usable - waste)
    reorder_qty = round_up(horizon_demand + safety - consumable, order_multiple)
    risk = risk_level(dts, lead_time_days)

    if dts is None:
        reorder_by, overdue = None, False
    else:
        days_until_order = dts - lead_time_days
        overdue = days_until_order <= 0
        reorder_by = today + timedelta(days=max(0, int(math.floor(days_until_order))))

    bridge_need = max(0.0, lead_demand + min_safety_stock - consumable)
    donor_required = demand_over(daily_hi, lead_time_days + 14) + min_safety_stock
    surplus = max(0.0, consumable - donor_required)

    if dts is None:
        reason = f"Stock covers the full {SIM_DAYS}-day projection; no stockout expected."
    elif risk == "critical":
        reason = f"Projected stockout in {dts:.0f} days is within the {lead_time_days}-day resupply lead time - a reorder placed today would arrive too late."
    elif risk == "high":
        reason = f"Projected stockout in {dts:.0f} days leaves under a week of margin over the {lead_time_days}-day lead time."
    elif risk == "moderate":
        reason = f"Projected stockout in {dts:.0f} days; reorder soon (lead time {lead_time_days} days)."
    else:
        reason = f"Projected stockout in {dts:.0f} days, comfortably beyond the {lead_time_days}-day lead time."

    def r(v: float | None, nd: int = 1):
        return None if v is None else round(v, nd)

    return {
        "stock_total": int(sum(b.quantity for b in batches)),
        "stock_usable": int(usable),
        "stock_expired": int(expired_units),
        "avg_daily_usage": r(historical_avg_daily, 2),
        "forecast_daily_next4w": r(sum(daily_c[:28]) / 28.0, 2),
        "days_cover_at_historical_rate": r(no_forecast["stockout_day"], 1),
        "days_to_stockout": r(dts),
        "days_to_stockout_pessimistic": r(pess["stockout_day"]),
        "days_to_stockout_optimistic": r(opt["stockout_day"]),
        "stockout_date": None if dts is None else (today + timedelta(days=int(dts))).isoformat(),
        "expected_expiry_waste": int(round(waste)),
        "lead_time_days": lead_time_days,
        "demand_over_lead_time": int(round(lead_demand)),
        "safety_stock_units": safety,
        "demand_over_reorder_horizon": int(round(horizon_demand)),
        "reorder_qty": reorder_qty,
        "reorder_by": None if reorder_by is None else reorder_by.isoformat(),
        "reorder_overdue": overdue,
        "risk": risk,
        "risk_reason": reason,
        "bridge_need_units": int(math.ceil(bridge_need)),
        "surplus_units": int(math.floor(surplus)),
        "forecast_extrapolated_beyond_horizon": extrapolated,
        "_daily_central": daily_c,
        "_daily_upper": daily_hi,
        "_batches": list(batches),
    }


def public(analysis: dict) -> dict:
    return {k: v for k, v in analysis.items() if not k.startswith("_")}
