from datetime import date, timedelta

import pytest

from pulsechain.services import inventory as inv
from pulsechain.services import redistribution as rd

TODAY = date(2026, 5, 25)


def make_pair(cid, lat, lon, batches, weekly, *, lead=14, safety=20, speed=30.0, med_id="act", cold=False, clinic_cold=False):
    analysis = inv.analyze_pair(
        batches=batches, today=TODAY, lead_time_days=lead, min_safety_stock=safety, order_multiple=10,
        weekly_expected=[weekly] * 8, weekly_lower=[weekly * 0.9] * 8, weekly_upper=[weekly * 1.15] * 8, historical_avg_daily=weekly / 7)
    return {
        "clinic_id": cid, "medicine_id": med_id, "lead_time_days": lead, "min_safety_stock": safety, "analysis": analysis,
        "clinic": {"id": cid, "name": f"Clinic {cid}", "lat": lat, "lon": lon, "road_speed_kmh": speed, "has_cold_chain": clinic_cold},
        "medicine": {"id": med_id, "name": "Test medicine", "unit": "units", "requires_cold_chain": cold},
    }


def B(qty, days, bid):
    return inv.Batch(bid, qty, TODAY + timedelta(days=days))


def network():
    return [
        make_pair("A", 20.0, 84.0, [B(1500, 500, 1)], 70),            # big surplus donor
        make_pair("B", 20.1, 84.1, [B(25, 500, 2)], 70),              # needy receiver (~2.5 days of stock)
        make_pair("C", 20.05, 84.05, [B(60, 500, 3)], 70),            # also short, no surplus
    ]


def test_proposes_transfer_from_surplus_to_shortage_and_requires_approval():
    t = rd.propose_transfers(network(), TODAY)
    assert t, "expected at least one recommendation"
    assert all(x["requires_human_approval"] is True and x["status"] == "proposed" for x in t)
    assert t[0]["donor_id"] == "A" and t[0]["receiver_id"] == "B"
    assert t[0]["receiver_days_to_stockout_after"] is None or t[0]["receiver_days_to_stockout_after"] > t[0]["receiver_days_to_stockout_before"]


def test_donor_is_never_taken_below_safety_stock_under_pessimistic_demand():
    pairs = network()
    for t in rd.propose_transfers(pairs, TODAY):
        donor = next(p for p in pairs if p["clinic_id"] == t["donor_id"])
        sent = {x["batch_id"]: x["quantity"] for x in t["batches"]}
        after = [inv.Batch(x.batch_id, x.quantity - sent.get(x.batch_id, 0), x.expiry) for x in donor["analysis"]["_batches"]]
        left = inv.remaining_after(after, donor["analysis"]["_daily_upper"], TODAY, donor["lead_time_days"] + rd.DONOR_EXTRA_COVER_DAYS)
        assert left >= donor["min_safety_stock"]


def test_expired_stock_is_never_transferred():
    pairs = [make_pair("A", 20.0, 84.0, [B(900, -5, "expired"), B(300, 500, "good")], 20), network()[1]]
    for t in rd.propose_transfers(pairs, TODAY):
        assert all(x["batch_id"] != "expired" for x in t["batches"])
        assert all(date.fromisoformat(x["expiry_date"]) > TODAY for x in t["batches"])


def test_short_dated_batches_are_excluded():
    pairs = [make_pair("A", 20.0, 84.0, [B(900, 20, "short"), B(300, 500, "long")], 20), network()[1]]
    for t in rd.propose_transfers(pairs, TODAY):
        for x in t["batches"]:
            assert (date.fromisoformat(x["expiry_date"]) - date.fromisoformat(t["arrival_date"])).days >= rd.MIN_SHELF_LIFE_DAYS
            assert x["batch_id"] != "short"


def test_too_far_donor_is_skipped():
    pairs = [make_pair("A", 20.0, 84.0, [B(1500, 500, 1)], 70), make_pair("B", 24.0, 88.0, [B(25, 500, 2)], 70)]  # ~600 km apart
    assert rd.propose_transfers(pairs, TODAY) == []


def test_transfer_that_arrives_after_stockout_is_skipped():
    # receiver has ~0.3 days of stock; a >1 day dispatch can't arrive in time
    pairs = [make_pair("A", 20.0, 84.0, [B(1500, 500, 1)], 70), make_pair("B", 20.1, 84.1, [B(3, 500, 2)], 70)]
    assert rd.propose_transfers(pairs, TODAY) == []


def test_cold_chain_medicine_only_goes_to_cold_chain_clinics():
    a = make_pair("A", 20.0, 84.0, [B(1500, 500, 1)], 70, cold=True, clinic_cold=True)
    b = make_pair("B", 20.1, 84.1, [B(25, 500, 2)], 70, cold=True, clinic_cold=False)
    assert rd.propose_transfers([a, b], TODAY) == []
    b["clinic"]["has_cold_chain"] = True
    assert rd.propose_transfers([a, b], TODAY)


def test_only_same_medicine_is_matched():
    a = make_pair("A", 20.0, 84.0, [B(1500, 500, 1)], 70, med_id="act")
    b = make_pair("B", 20.1, 84.1, [B(25, 500, 2)], 70, med_id="ors")
    assert rd.propose_transfers([a, b], TODAY) == []


def test_receiver_gets_no_more_than_its_gap_and_donor_no_more_than_surplus():
    pairs = network()
    ts = rd.propose_transfers(pairs, TODAY)
    for t in ts:
        recv = next(p for p in pairs if p["clinic_id"] == t["receiver_id"])
        assert t["quantity"] <= recv["analysis"]["bridge_need_units"]
    per_donor = {}
    for t in ts:
        per_donor[t["donor_id"]] = per_donor.get(t["donor_id"], 0) + t["quantity"]
    for d, q in per_donor.items():
        assert q <= next(p for p in pairs if p["clinic_id"] == d)["analysis"]["surplus_units"]


def test_haversine_known_distance():
    # roughly 111 km per degree of latitude
    assert rd.haversine_km(20.0, 84.0, 21.0, 84.0) == pytest.approx(111.2, abs=0.5)


def test_donor_safety_is_enforced_even_if_the_surplus_estimate_is_wrong():
    """Defence in depth: inflate the donor's reported surplus and the engine must still not over-draw it."""
    pairs = network()
    donor = pairs[0]
    donor["analysis"]["surplus_units"] = 10_000  # simulate a bug / stale number upstream
    donor["analysis"]["_batches"] = [B(600, 500, 1)]  # own need over cover period is ~340, so true surplus is only ~260
    pairs[1]["analysis"]["bridge_need_units"] = 5_000  # receiver wants far more than the donor can safely give
    transfers = rd.propose_transfers(pairs, TODAY)
    sent = sum(t["quantity"] for t in transfers if t["donor_id"] == "A")
    remaining = inv.remaining_after([inv.Batch(1, 600 - sent, TODAY + timedelta(days=500))], donor["analysis"]["_daily_upper"],
                                    TODAY, donor["lead_time_days"] + rd.DONOR_EXTRA_COVER_DAYS)
    assert sent > 0, "the donor has a real surplus, so some transfer is expected"
    assert remaining >= donor["min_safety_stock"]
    assert sent < 600


def test_donor_remains_safe_unit():
    p = make_pair("A", 20.0, 84.0, [B(400, 500, 1)], 70)  # 10 units/day demand
    horizon_need = 10 * (p["lead_time_days"] + rd.DONOR_EXTRA_COVER_DAYS) * 1.15 + p["min_safety_stock"]
    assert rd.donor_remains_safe(p, [B(int(horizon_need) + 5, 500, 1)], TODAY)
    assert not rd.donor_remains_safe(p, [B(int(horizon_need) - 60, 500, 1)], TODAY)
