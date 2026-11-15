"""Village-to-village redistribution recommender (pure functions, no I/O).

Produces *recommendations only*. Each proposal carries `requires_human_approval = True`; nothing moves
until a person approves it in the UI/API (and even then the demo only updates simulated inventory).

Hard safety rules (enforced in code and covered by tests)
---------------------------------------------------------
1. Same medicine only (same formulation/strength id); cold-chain medicines only go to cold-chain clinics.
2. Expired batches are never sent. Batches must still have >= MIN_SHELF_LIFE_DAYS of life on ARRIVAL.
3. A donor is never taken below its own requirement: after the transfer, with the *pessimistic* (upper-bound)
   demand projection, it must still hold at least its minimum safety stock after covering its own resupply
   lead time + 14 days.
4. The transfer must be physically feasible: road distance <= MAX_DISTANCE_KM, travel time <= MAX_TRAVEL_HOURS,
   and it must arrive BEFORE the receiver's projected stockout.
5. Receivers only get what bridges their gap until their own reorder arrives (no hoarding).

Matching: receivers are served most-urgent first; each takes from the nearest feasible donors until its gap is closed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from .inventory import RISK_ORDER, Batch, remaining_after, simulate

MIN_SHELF_LIFE_DAYS = 45
MAX_DISTANCE_KM = 150.0
MAX_TRAVEL_HOURS = 8.0
CIRCUITY_FACTOR = 1.3  # road distance vs straight line
DISPATCH_DAYS = 1.0  # time to pack and dispatch
MIN_TRANSFER_UNITS = 10
DONOR_EXTRA_COVER_DAYS = 14


@dataclass(frozen=True)
class RedistributionConfig:
    min_shelf_life_days: int = MIN_SHELF_LIFE_DAYS
    max_distance_km: float = MAX_DISTANCE_KM
    max_travel_hours: float = MAX_TRAVEL_HOURS
    min_transfer_units: int = MIN_TRANSFER_UNITS
    min_receiver_risk: str = "moderate"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def road_distance_km(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    return haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) * CIRCUITY_FACTOR


def eligible_batches(batches: Sequence[Batch], arrival: date, min_shelf_life_days: int) -> list[Batch]:
    """Batches that are unexpired on arrival and keep at least the minimum shelf life, FEFO order."""
    ok = [b for b in batches if b.quantity > 0 and (b.expiry - arrival).days >= min_shelf_life_days]
    return sorted(ok, key=lambda b: b.expiry)


def _take_fefo(batches: Sequence[Batch], qty: int) -> list[dict]:
    taken, remaining = [], qty
    for b in batches:
        if remaining <= 0:
            break
        q = min(b.quantity, remaining)
        if q > 0:
            taken.append({"batch_id": b.batch_id, "quantity": q, "expiry_date": b.expiry.isoformat()})
            remaining -= q
    return taken


def _minus(batches: Sequence[Batch], taken: Sequence[dict]) -> list[Batch]:
    sent = {t["batch_id"]: t["quantity"] for t in taken}
    return [Batch(b.batch_id, b.quantity - sent.get(b.batch_id, 0), b.expiry) for b in batches]


def donor_remains_safe(donor: Mapping[str, Any], batches_after: Sequence[Batch], today: date) -> bool:
    horizon = donor["lead_time_days"] + DONOR_EXTRA_COVER_DAYS
    left = remaining_after(batches_after, donor["analysis"]["_daily_upper"], today, horizon)
    return left >= donor["min_safety_stock"]


def propose_transfers(pairs: Sequence[Mapping[str, Any]], today: date,
                      config: RedistributionConfig = RedistributionConfig()) -> list[dict]:
    """`pairs`: one dict per (clinic, medicine) with keys: clinic_id, clinic (dict with lat/lon/name/
    road_speed_kmh/has_cold_chain), medicine_id, medicine (dict), lead_time_days, min_safety_stock and
    `analysis` (the dict from inventory.analyze_pair incl. private keys)."""
    by_med: dict[str, list[Mapping[str, Any]]] = {}
    for p in pairs:
        by_med.setdefault(p["medicine_id"], []).append(p)

    proposals: list[dict] = []
    min_rank = RISK_ORDER[config.min_receiver_risk]
    for medicine_id, group in by_med.items():
        receivers = [p for p in group if RISK_ORDER[p["analysis"]["risk"]] >= min_rank and p["analysis"]["bridge_need_units"] > 0]
        receivers.sort(key=lambda p: (p["analysis"]["days_to_stockout"] if p["analysis"]["days_to_stockout"] is not None else 1e9))
        surplus_left = {p["clinic_id"]: p["analysis"]["surplus_units"] for p in group}
        batches_left = {p["clinic_id"]: list(p["analysis"]["_batches"]) for p in group}

        for rcv in receivers:
            need = rcv["analysis"]["bridge_need_units"]
            dts = rcv["analysis"]["days_to_stockout"]
            donors = sorted(
                (d for d in group if d["clinic_id"] != rcv["clinic_id"] and surplus_left[d["clinic_id"]] >= 1),
                key=lambda d: road_distance_km(d["clinic"], rcv["clinic"]))
            for dnr in donors:
                if need <= 0:
                    break
                medicine = dnr["medicine"]
                if medicine.get("requires_cold_chain") and not rcv["clinic"].get("has_cold_chain", False):
                    continue
                dist = road_distance_km(dnr["clinic"], rcv["clinic"])
                hours = dist / max(1.0, dnr["clinic"]["road_speed_kmh"])
                arrival_days = DISPATCH_DAYS + hours / 24.0
                if dist > config.max_distance_km or hours > config.max_travel_hours:
                    continue
                if dts is not None and arrival_days >= dts:
                    continue  # would arrive after the receiver has already run out
                arrival = today + timedelta(days=math.ceil(arrival_days))
                pool = eligible_batches(batches_left[dnr["clinic_id"]], arrival, config.min_shelf_life_days)
                avail = min(surplus_left[dnr["clinic_id"]], sum(b.quantity for b in pool))
                qty = int(min(need, avail))
                # Shrink until the donor provably stays safe under pessimistic demand.
                while qty >= 1:
                    taken = _take_fefo(pool, qty)
                    if donor_remains_safe(dnr, _minus(batches_left[dnr["clinic_id"]], taken), today):
                        break
                    qty = int(qty * 0.9) if qty > 10 else qty - 1
                if qty < 1 or (qty < config.min_transfer_units and qty < need):
                    continue
                taken = _take_fefo(pool, qty)

                after_batches = [*rcv["analysis"]["_batches"],
                                 *[Batch(f"in-{t['batch_id']}", t["quantity"], date.fromisoformat(t["expiry_date"])) for t in taken]]
                after = simulate(after_batches, rcv["analysis"]["_daily_central"], today)
                donor_after = remaining_after(_minus(batches_left[dnr["clinic_id"]], taken),
                                              dnr["analysis"]["_daily_upper"], today, dnr["lead_time_days"] + DONOR_EXTRA_COVER_DAYS)

                proposals.append({
                    "id": f"{medicine_id}:{dnr['clinic_id']}->{rcv['clinic_id']}",
                    "medicine_id": medicine_id,
                    "medicine_name": medicine["name"],
                    "unit": medicine["unit"],
                    "donor_id": dnr["clinic_id"], "donor_name": dnr["clinic"]["name"],
                    "receiver_id": rcv["clinic_id"], "receiver_name": rcv["clinic"]["name"],
                    "quantity": qty,
                    "batches": taken,
                    "distance_km": round(dist, 1),
                    "travel_hours": round(hours, 1),
                    "arrival_date": arrival.isoformat(),
                    "receiver_risk_before": rcv["analysis"]["risk"],
                    "receiver_days_to_stockout_before": dts,
                    "receiver_days_to_stockout_after": None if after["stockout_day"] is None else round(after["stockout_day"], 1),
                    "donor_surplus_before": surplus_left[dnr["clinic_id"]],
                    "donor_stock_after_cover_period": int(donor_after),
                    "donor_min_safety_stock": dnr["min_safety_stock"],
                    "rationale": (f"{dnr['clinic']['name']} holds surplus {medicine['name']} beyond its own projected needs; "
                                  f"{rcv['clinic']['name']} is projected to run out in "
                                  f"{'under a day' if dts is not None and dts < 1 else f'{dts:.0f} days'} versus a {rcv['lead_time_days']}-day resupply. "
                                  f"{dist:.0f} km by road (~{hours:.1f} h)."),
                    "status": "proposed",
                    "requires_human_approval": True,
                })
                surplus_left[dnr["clinic_id"]] -= qty
                batches_left[dnr["clinic_id"]] = _minus(batches_left[dnr["clinic_id"]], taken)
                need -= qty
    proposals.sort(key=lambda t: (t["receiver_days_to_stockout_before"] if t["receiver_days_to_stockout_before"] is not None else 1e9, t["distance_km"]))
    return proposals
