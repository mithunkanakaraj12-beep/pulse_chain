from datetime import date, timedelta

import pytest

from pulsechain.services import inventory as inv

TODAY = date(2026, 5, 25)


def b(qty, days, bid="b"):
    return inv.Batch(bid, qty, TODAY + timedelta(days=days))


def analyze(batches, weekly, lead=14, safety=10, multiple=10, hist=10.0, spread=0.2):
    return inv.analyze_pair(
        batches=batches, today=TODAY, lead_time_days=lead, min_safety_stock=safety, order_multiple=multiple,
        weekly_expected=weekly, weekly_lower=[w * (1 - spread) for w in weekly], weekly_upper=[w * (1 + spread) for w in weekly],
        historical_avg_daily=hist)


def test_stockout_day_is_fractional_and_exact():
    daily = [10.0] * 60
    res = inv.simulate([b(100, 400)], daily, TODAY)
    assert res["stockout_day"] == pytest.approx(10.0)  # 100 units / 10 per day


def test_no_stockout_when_stock_exceeds_demand():
    assert inv.simulate([b(10_000, 400)], [5.0] * 180, TODAY)["stockout_day"] is None


def test_expired_batches_are_never_usable():
    assert inv.total_usable([b(50, -1), b(30, 0), b(20, 5)], TODAY) == 20  # expiry == today counts as expired
    a = analyze([b(500, -3), b(70, 400)], [70] * 8)
    assert a["stock_expired"] == 500 and a["stock_usable"] == 70


def test_fefo_wastes_units_that_expire_before_use():
    # 100 units expire in 5 days; demand 5/day -> only 25 used, 75 wasted
    res = inv.simulate([b(100, 5, "old"), b(1000, 400, "new")], [5.0] * 30, TODAY)
    assert res["wasted"] == pytest.approx(75.0)
    assert res["stockout_day"] is None


def test_surge_shortens_projected_cover():
    calm = analyze([b(300, 400)], [70] * 8)
    surge = analyze([b(300, 400)], [70, 70, 140, 210, 210, 210, 210, 210])
    assert surge["days_to_stockout"] < calm["days_to_stockout"]
    assert surge["risk"] != "low"


def test_pessimistic_is_earlier_than_central_than_optimistic():
    a = analyze([b(300, 400)], [70] * 8, spread=0.3)
    assert a["days_to_stockout_pessimistic"] < a["days_to_stockout"] < a["days_to_stockout_optimistic"]


@pytest.mark.parametrize("dts,lead,expected", [
    (None, 14, "low"), (14, 14, "critical"), (10, 14, "critical"), (14.1, 14, "high"), (21, 14, "high"),
    (21.1, 14, "moderate"), (35, 14, "moderate"), (35.1, 14, "low"),
])
def test_risk_thresholds_are_relative_to_lead_time(dts, lead, expected):
    assert inv.risk_level(dts, lead) == expected


def test_reorder_quantity_is_transparent_and_rounded_up():
    a = analyze([b(100, 400)], [70] * 8, lead=14, safety=10, multiple=25, spread=0.0)
    # horizon = 14 + 28 = 42 days at 10/day = 420; safety 10; stock 100 -> 330 -> rounded up to 350
    assert a["demand_over_reorder_horizon"] == 420
    assert a["reorder_qty"] == 350


def test_reorder_zero_when_overstocked():
    assert analyze([b(5000, 600)], [70] * 8)["reorder_qty"] == 0


def test_safety_stock_uses_uncertainty_buffer_when_larger_than_policy():
    a = analyze([b(300, 400)], [70] * 8, lead=14, safety=5, spread=0.5)
    assert a["safety_stock_units"] > 5  # (upper - central) over lead time = 0.5 * 140 = 70


def test_reorder_date_is_stockout_minus_lead_and_overdue_flag():
    ok = analyze([b(600, 400)], [70] * 8, lead=14, spread=0.0)  # 60 days of stock
    assert ok["reorder_by"] == (TODAY + timedelta(days=46)).isoformat() and not ok["reorder_overdue"]
    late = analyze([b(100, 400)], [70] * 8, lead=14, spread=0.0)  # 10 days of stock
    assert late["reorder_overdue"] and late["reorder_by"] == TODAY.isoformat() and late["risk"] == "critical"


def test_zero_stock_is_immediate_stockout():
    a = analyze([], [70] * 8)
    assert a["days_to_stockout"] == 0.0 and a["risk"] == "critical"


def test_zero_demand_never_stocks_out():
    a = analyze([b(10, 400)], [0] * 8, hist=0.0)
    assert a["days_to_stockout"] is None and a["risk"] == "low" and a["reorder_qty"] == 0


def test_forecast_beyond_horizon_is_flagged_as_extrapolated():
    assert analyze([b(10, 400)], [70] * 8)["forecast_extrapolated_beyond_horizon"] is True
