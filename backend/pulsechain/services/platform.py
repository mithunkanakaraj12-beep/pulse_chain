"""Application service layer: loads data, runs forecasts, and assembles inventory/alert/transfer views.

`Platform` is framework-agnostic (the Flask app is a thin shell over it), so the same service could sit
behind FastAPI or a CLI. Forecasts are cached per (scenario, model version); everything derived from
inventory is recomputed on each call so edits in the demo ripple through immediately.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .. import db
from ..config import Settings
from ..domain import (
    CLINIC_BY_ID, DATA_LABEL, DISEASES, FORECAST_MAX_WEEKS, MEDICINE_BY_ID, MEDICINES, REGIONS,
    TREATMENT_PROTOCOL, demo_today,
)
from ..ml.inference import Forecaster, ModelNotTrainedError
from . import alerts as alerts_mod
from . import inventory as inv
from . import redistribution as redis_mod
from . import scenario as scen
from . import sms as sms_mod
from . import weather as weather_mod

HISTORY_WEEKS_SHOWN = 26
FRAME_WEEKS = 30
DISEASE_NAME = {d.id: d.name for d in DISEASES}


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class ValidationError(ValueError):
    pass


def _iso(d: date) -> str:
    return d.isoformat()


class Platform:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._forecaster: Forecaster | None = None
        self._forecast_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ plumbing
    def session(self):
        return db.session(self.settings.db_path)

    @property
    def forecaster(self) -> Forecaster:
        with self._lock:
            if self._forecaster is None:
                self._forecaster = Forecaster.load(self.settings.model_dir)
            return self._forecaster

    def reload_model(self) -> None:
        with self._lock:
            self._forecaster = None
            self._forecast_cache.clear()

    def model_ready(self) -> bool:
        try:
            _ = self.forecaster
            return True
        except ModelNotTrainedError:
            return False

    def clear_caches(self) -> None:
        with self._lock:
            self._forecast_cache.clear()

    # ------------------------------------------------------------------ meta / settings
    def as_of(self, conn) -> date:
        v = db.get_meta(conn, "as_of")
        if not v:
            raise NotFoundError("Database is empty. Run `python -m pulsechain.cli reset`.")
        return date.fromisoformat(v)

    def get_scenario(self, conn) -> dict:
        return db.get_meta(conn, "scenario", dict(scen.DEFAULT_SCENARIO))

    def set_scenario(self, payload: dict) -> dict:
        new = scen.validate(payload)
        with self.session() as conn:
            db.set_meta(conn, "scenario", new)
        return new

    def alert_threshold(self, conn) -> str:
        return db.get_meta(conn, "alert_threshold", self.settings.alert_risk_threshold)

    def set_alert_threshold(self, level: str) -> str:
        if level not in ("moderate", "high", "critical"):
            raise ValidationError("threshold must be one of: moderate, high, critical")
        with self.session() as conn:
            db.set_meta(conn, "alert_threshold", level)
        return level

    def meta(self) -> dict:
        with self.session() as conn:
            as_of = self.as_of(conn)
            metrics = self.forecaster.metrics if self.model_ready() else {}
            return {
                "app": "PulseChain AI",
                "data_label": DATA_LABEL,
                "data_is_simulated": True,
                "as_of": _iso(as_of), "demo_today": _iso(demo_today(as_of)),
                "scenario": self.get_scenario(conn),
                "alert_threshold": self.alert_threshold(conn),
                "model_ready": self.model_ready(),
                "model": {"trained_at": metrics.get("trained_at"), "description": metrics.get("model_description")} if metrics else None,
                "sms": {"mode": "simulated", "live_available": self.settings.live_sms_possible,
                        "configured_mode": self.settings.sms_mode},
                "weather": {"open_meteo_enabled": self.settings.open_meteo_enabled},
                "regions": [{"id": k, "name": v} for k, v in REGIONS.items()],
                "diseases": [{"id": d.id, "name": d.name, "description": d.description} for d in DISEASES],
                "medicines": [{"id": m.id, "name": m.name, "short_name": m.short_name, "unit": m.unit} for m in MEDICINES],
            }

    # ------------------------------------------------------------------ data access
    def clinics(self, conn) -> list[dict]:
        out = db.rows(conn, "SELECT * FROM clinics ORDER BY id")
        for c in out:
            c["has_cold_chain"] = False
            c["region_name"] = REGIONS.get(c["region"], c["region"])
        return out

    def _recent_frames(self, conn, as_of: date, scenario: dict) -> dict[str, dict[str, pd.DataFrame]]:
        """{disease: {clinic_id: frame}} with the last FRAME_WEEKS weeks, scenario overlay applied."""
        start = _iso(as_of - timedelta(weeks=FRAME_WEEKS - 1))
        cases = db.frame(conn, "SELECT clinic_id, disease, week_start, cases FROM cases WHERE week_start >= ?", (start,))
        weather = db.frame(conn, "SELECT clinic_id, week_start, rain_mm, temp_c FROM weather WHERE week_start >= ?", (start,))
        clinics = db.frame(conn, "SELECT id AS clinic_id, region, catchment_pop FROM clinics")
        base = cases.merge(weather, on=["clinic_id", "week_start"]).merge(clinics, on="clinic_id")
        base["week_start"] = pd.to_datetime(base["week_start"])
        out: dict[str, dict[str, pd.DataFrame]] = {}
        for (disease, clinic_id), g in base.groupby(["disease", "clinic_id"]):
            region = g["region"].iloc[0]
            frame = g.drop(columns=["clinic_id", "disease", "region"]).sort_values("week_start").reset_index(drop=True)
            out.setdefault(disease, {})[clinic_id] = scen.apply_overlay(frame, region, scenario)
        return out

    def forecast_all(self, conn, scenario: dict | None = None) -> dict[tuple[str, str], dict]:
        """Forecast every (clinic, disease). Cached per scenario signature + model version."""
        as_of = self.as_of(conn)
        scenario = scenario if scenario is not None else self.get_scenario(conn)
        key = f"{self.forecaster.version}|{as_of}|{scen.signature(scenario)}"
        with self._lock:
            cached = self._forecast_cache.get(key)
        if cached is not None:
            return cached
        frames = self._recent_frames(conn, as_of, scenario)
        result: dict[tuple[str, str], dict] = {}
        for disease, per_clinic in frames.items():
            for clinic_id, frame in per_clinic.items():
                result[(clinic_id, disease)] = self.forecaster.forecast(disease, frame, weeks=FORECAST_MAX_WEEKS, explain=True)
        with self._lock:
            self._forecast_cache[key] = result
        return result

    # ------------------------------------------------------------------ demand + pair analysis
    @staticmethod
    def weekly_demand(clinic: dict, medicine_id: str, forecasts: dict) -> tuple[list[float], list[float], list[float]]:
        med = MEDICINE_BY_ID[medicine_id]
        base = med.baseline_weekly_per_1000 * clinic["catchment_pop"] / 1000.0
        exp = [base] * FORECAST_MAX_WEEKS
        lo = [base] * FORECAST_MAX_WEEKS
        hi = [base] * FORECAST_MAX_WEEKS
        for disease, rate in TREATMENT_PROTOCOL[medicine_id].items():
            fc = forecasts[(clinic["id"], disease)]["weeks"]
            for i in range(FORECAST_MAX_WEEKS):
                exp[i] += fc[i]["expected"] * rate
                lo[i] += fc[i]["lower"] * rate
                hi[i] += fc[i]["upper"] * rate
        return exp, lo, hi

    def build_pairs(self, conn, scenario: dict | None = None) -> list[dict]:
        as_of = self.as_of(conn)
        today = demo_today(as_of)
        scenario = scenario if scenario is not None else self.get_scenario(conn)
        forecasts = self.forecast_all(conn, scenario)
        clinics = self.clinics(conn)
        policy = {(r["clinic_id"], r["medicine_id"]): r["min_safety_stock"] for r in db.rows(conn, "SELECT * FROM inventory_policy")}
        batches: dict[tuple[str, str], list[inv.Batch]] = defaultdict(list)
        for r in db.rows(conn, "SELECT * FROM inventory_batches"):
            batches[(r["clinic_id"], r["medicine_id"])].append(inv.Batch(r["id"], r["quantity"], date.fromisoformat(r["expiry_date"])))
        since = _iso(as_of - timedelta(weeks=7))
        usage = {(r["clinic_id"], r["medicine_id"]): r["avg_week"] for r in db.rows(
            conn, "SELECT clinic_id, medicine_id, AVG(units) AS avg_week FROM consumption WHERE week_start >= ? GROUP BY 1, 2", (since,))}
        pairs = []
        for c in clinics:
            for m in MEDICINES:
                exp, lo, hi = self.weekly_demand(c, m.id, forecasts)
                min_safety = policy.get((c["id"], m.id), 10)
                analysis = inv.analyze_pair(
                    batches=batches.get((c["id"], m.id), []), today=today, lead_time_days=c["lead_time_days"],
                    min_safety_stock=min_safety, order_multiple=m.order_multiple, weekly_expected=exp,
                    weekly_lower=lo, weekly_upper=hi, historical_avg_daily=usage.get((c["id"], m.id), 0.0) / 7.0)
                analysis["weekly_demand_expected"] = [round(v, 1) for v in exp]
                analysis["weekly_demand_lower"] = [round(v, 1) for v in lo]
                analysis["weekly_demand_upper"] = [round(v, 1) for v in hi]
                pairs.append({
                    "clinic_id": c["id"], "clinic": c, "medicine_id": m.id,
                    "medicine": {"id": m.id, "name": m.name, "short_name": m.short_name, "unit": m.unit,
                                 "requires_cold_chain": m.requires_cold_chain},
                    "lead_time_days": c["lead_time_days"], "min_safety_stock": min_safety, "analysis": analysis,
                })
        return pairs

    def _surges(self, conn, scenario: dict | None) -> list[dict]:
        forecasts = self.forecast_all(conn, scenario)
        clinics = {c["id"]: c for c in self.clinics(conn)}
        out = []
        for (clinic_id, disease), fc in forecasts.items():
            out.append({"clinic_id": clinic_id, "clinic": clinics[clinic_id], "disease": disease,
                        "disease_name": DISEASE_NAME[disease], "signal": alerts_mod.surge_signal(fc["weeks"])})
        return out

    def _rejected_ids(self, conn) -> set[str]:
        return {r["proposal_key"] for r in db.rows(conn, "SELECT proposal_key FROM transfers WHERE status = 'rejected'")}

    def snapshot(self, scenario: dict | None = None, *, apply_decisions: bool = True) -> dict:
        """Everything derived from current data + scenario. `scenario` overrides the stored one (what-if walkthroughs)."""
        with self.session() as conn:
            as_of = self.as_of(conn)
            today = demo_today(as_of)
            stored = self.get_scenario(conn)
            scenario = scenario if scenario is not None else stored
            pairs = self.build_pairs(conn, scenario)
            surges = self._surges(conn, scenario)
            proposals = redis_mod.propose_transfers(pairs, today)
            if apply_decisions:
                rejected = self._rejected_ids(conn)
                proposals = [p for p in proposals if p["id"] not in rejected]
            transfer_by_receiver = {}
            for t in proposals:
                transfer_by_receiver.setdefault((t["receiver_id"], t["medicine_id"]), t["donor_name"])
            ack = {r["alert_key"] for r in db.rows(conn, "SELECT alert_key FROM alert_state")}
            alert_list = alerts_mod.build_alerts(pairs=pairs, surges=surges, today=today, acknowledged=ack,
                                                 sms_threshold=self.alert_threshold(conn), transfer_by_receiver=transfer_by_receiver)
            sent = {}
            for r in db.rows(conn, "SELECT alert_key, mode, status, created_at FROM sms_log ORDER BY id"):
                sent[r["alert_key"]] = {"mode": r["mode"], "status": r["status"], "at": r["created_at"]}
            for a in alert_list:
                a["last_sms"] = sent.get(a["key"])
            return {"as_of": as_of, "today": today, "scenario": scenario, "pairs": pairs, "surges": surges,
                    "transfers": proposals, "alerts": alert_list}

    # ------------------------------------------------------------------ public views
    @staticmethod
    def _pair_row(p: dict) -> dict:
        a = inv.public(p["analysis"])
        return {"clinic_id": p["clinic_id"], "clinic_name": p["clinic"]["name"], "region": p["clinic"]["region"],
                "medicine_id": p["medicine_id"], "medicine_name": p["medicine"]["name"], "short_name": p["medicine"]["short_name"],
                "unit": p["medicine"]["unit"], "min_safety_stock": p["min_safety_stock"], **a}

    def inventory_rows(self, *, clinic_id: str | None = None, scenario: dict | None = None) -> list[dict]:
        snap = self.snapshot(scenario)
        rows = [self._pair_row(p) for p in snap["pairs"] if clinic_id in (None, p["clinic_id"])]
        with self.session() as conn:
            batch_rows = db.rows(conn, "SELECT id, clinic_id, medicine_id, quantity, expiry_date, received_date, origin FROM inventory_batches ORDER BY expiry_date")
        by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
        today = snap["today"]
        for b in batch_rows:
            b["expired"] = date.fromisoformat(b["expiry_date"]) <= today
            by_pair[(b["clinic_id"], b["medicine_id"])].append(b)
        for r in rows:
            r["batches"] = by_pair.get((r["clinic_id"], r["medicine_id"]), [])
        return rows

    # ------------------------------------------------------------------ inventory edits
    @staticmethod
    def _check_qty(q: Any) -> int:
        if isinstance(q, bool) or not isinstance(q, int) or q < 0 or q > 1_000_000:
            raise ValidationError("quantity must be an integer between 0 and 1,000,000")
        return q

    @staticmethod
    def _check_date(v: Any) -> str:
        try:
            d = date.fromisoformat(str(v))
        except ValueError as exc:
            raise ValidationError("expiry_date must be an ISO date (YYYY-MM-DD)") from exc
        if d.year < 2000 or d.year > 2100:
            raise ValidationError("expiry_date out of range")
        return d.isoformat()

    def update_batch(self, batch_id: int, payload: dict) -> dict:
        with self.session() as conn:
            row = db.one(conn, "SELECT * FROM inventory_batches WHERE id = ?", (batch_id,))
            if not row:
                raise NotFoundError(f"batch {batch_id} not found")
            qty = self._check_qty(payload["quantity"]) if "quantity" in payload else row["quantity"]
            expiry = self._check_date(payload["expiry_date"]) if "expiry_date" in payload else row["expiry_date"]
            conn.execute("UPDATE inventory_batches SET quantity = ?, expiry_date = ? WHERE id = ?", (qty, expiry, batch_id))
            return db.one(conn, "SELECT * FROM inventory_batches WHERE id = ?", (batch_id,))

    def add_batch(self, payload: dict) -> dict:
        clinic_id, medicine_id = payload.get("clinic_id"), payload.get("medicine_id")
        if clinic_id not in CLINIC_BY_ID or medicine_id not in MEDICINE_BY_ID:
            raise ValidationError("unknown clinic_id or medicine_id")
        qty = self._check_qty(payload.get("quantity"))
        expiry = self._check_date(payload.get("expiry_date"))
        with self.session() as conn:
            today = demo_today(self.as_of(conn))
            cur = conn.execute(
                "INSERT INTO inventory_batches(clinic_id, medicine_id, quantity, expiry_date, received_date, origin) VALUES (?,?,?,?,?, 'manual')",
                (clinic_id, medicine_id, qty, expiry, _iso(today)))
            return db.one(conn, "SELECT * FROM inventory_batches WHERE id = ?", (cur.lastrowid,))

    def update_policy(self, clinic_id: str, medicine_id: str, payload: dict) -> dict:
        if clinic_id not in CLINIC_BY_ID or medicine_id not in MEDICINE_BY_ID:
            raise NotFoundError("unknown clinic or medicine")
        value = self._check_qty(payload.get("min_safety_stock"))
        with self.session() as conn:
            conn.execute("INSERT INTO inventory_policy VALUES (?,?,?) ON CONFLICT(clinic_id, medicine_id) DO UPDATE SET min_safety_stock = excluded.min_safety_stock",
                         (clinic_id, medicine_id, value))
        return {"clinic_id": clinic_id, "medicine_id": medicine_id, "min_safety_stock": value}

    # ------------------------------------------------------------------ transfers (human approval)
    def list_transfers(self, scenario: dict | None = None) -> dict:
        snap = self.snapshot(scenario)
        with self.session() as conn:
            decided = db.rows(conn, "SELECT * FROM transfers ORDER BY id DESC")
        for d in decided:
            d["batches"] = json.loads(d.pop("batches_json"))
            d["donor_name"] = CLINIC_BY_ID[d["donor_id"]].name
            d["receiver_name"] = CLINIC_BY_ID[d["receiver_id"]].name
            d["medicine_name"] = MEDICINE_BY_ID[d["medicine_id"]].name
        return {"proposed": snap["transfers"], "decided": decided, "clinics": self._clinic_points(snap),
                "notice": "Recommendations only. Approving a transfer in this demo updates SIMULATED inventory; no medicine or message is sent."}

    def _clinic_points(self, snap: dict) -> list[dict]:
        worst: dict[str, str] = {}
        surplus: dict[str, int] = defaultdict(int)
        for p in snap["pairs"]:
            cid, a = p["clinic_id"], p["analysis"]
            if inv.RISK_ORDER[a["risk"]] >= inv.RISK_ORDER.get(worst.get(cid, "low"), 0):
                worst[cid] = a["risk"]
            surplus[cid] += a["surplus_units"]
        out = []
        for c in {p["clinic_id"]: p["clinic"] for p in snap["pairs"]}.values():
            out.append({"id": c["id"], "name": c["name"], "region": c["region"], "lat": c["lat"], "lon": c["lon"],
                        "catchment_pop": c["catchment_pop"], "worst_risk": worst.get(c["id"], "low"),
                        "surplus_units": surplus[c["id"]]})
        return out

    def decide_transfer(self, proposal_id: str, approve: bool, note: str | None = None) -> dict:
        snap = self.snapshot()
        match = next((t for t in snap["transfers"] if t["id"] == proposal_id), None)
        if match is None:
            raise ConflictError("That recommendation is no longer valid (stock or forecast changed). Refresh and review again.")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.session() as conn:
            conn.execute(
                "INSERT INTO transfers(proposal_key, medicine_id, donor_id, receiver_id, quantity, batches_json, status, decided_at, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (match["id"], match["medicine_id"], match["donor_id"], match["receiver_id"], match["quantity"],
                 json.dumps(match["batches"]), "approved" if approve else "rejected", now, (note or "")[:200]))
            if approve:
                for b in match["batches"]:
                    updated = conn.execute(
                        "UPDATE inventory_batches SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
                        (b["quantity"], b["batch_id"], b["quantity"]))
                    if updated.rowcount != 1:
                        raise ConflictError("Donor stock changed while approving; nothing was moved.")
                    conn.execute(
                        "INSERT INTO inventory_batches(clinic_id, medicine_id, quantity, expiry_date, received_date, origin) VALUES (?,?,?,?,?, 'transfer')",
                        (match["receiver_id"], match["medicine_id"], b["quantity"], b["expiry_date"], match["arrival_date"]))
        return {"id": proposal_id, "status": "approved" if approve else "rejected", "simulated": True, "decided_at": now}

    # ------------------------------------------------------------------ alerts + SMS
    def alerts(self, scenario: dict | None = None) -> dict:
        snap = self.snapshot(scenario)
        counts = defaultdict(int)
        for a in snap["alerts"]:
            if not a["acknowledged"]:
                counts[a["type"]] += 1
        return {"alerts": snap["alerts"], "counts": dict(counts), "active": sum(counts.values())}

    def acknowledge(self, key: str) -> dict:
        snap = self.snapshot()
        if not any(a["key"] == key for a in snap["alerts"]):
            raise NotFoundError("alert not found")
        with self.session() as conn:
            conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?, ?)", (key, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return {"key": key, "acknowledged": True}

    def send_sms(self, alert_key: str, mode: str, confirm: bool, session=None) -> dict:
        snap = self.snapshot()
        alert = next((a for a in snap["alerts"] if a["key"] == alert_key), None)
        if alert is None or not alert.get("sms_preview"):
            raise NotFoundError("no SMS-eligible alert with that key")
        result = sms_mod.deliver(self.settings, to_number=alert["phone"], body=alert["sms_preview"], mode=mode,
                                 confirm=confirm, session=session)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.session() as conn:
            conn.execute(
                "INSERT INTO sms_log(created_at, alert_key, clinic_id, to_number, body, mode, status, provider_sid, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (now, alert_key, alert["clinic_id"], alert["phone"], alert["sms_preview"], result.mode, result.status, result.provider_sid, result.error))
        return {"alert_key": alert_key, "to": alert["phone"], "to_name": alert["health_worker"], "body": alert["sms_preview"],
                "chars": len(alert["sms_preview"]), "mode": result.mode, "status": result.status, "error": result.error, "sent_at": now}

    def sms_log(self, limit: int = 50) -> list[dict]:
        with self.session() as conn:
            out = db.rows(conn, "SELECT * FROM sms_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in out:
            r["clinic_name"] = CLINIC_BY_ID[r["clinic_id"]].name
        return out

    # ------------------------------------------------------------------ weather connector
    def weather_compare(self, clinic_id: str) -> dict:
        if clinic_id not in CLINIC_BY_ID:
            raise NotFoundError("unknown clinic")
        c = CLINIC_BY_ID[clinic_id]
        with self.session() as conn:
            as_of = self.as_of(conn)
            start = as_of - timedelta(weeks=7)
            syn = db.rows(conn, "SELECT week_start, rain_mm, temp_c FROM weather WHERE clinic_id = ? AND week_start >= ? ORDER BY week_start",
                          (clinic_id, _iso(start)))
        if not self.settings.open_meteo_enabled:
            return {"available": False, "reason": "Live weather is disabled (OPEN_METEO_ENABLED=false). Forecasts use the synthetic weather dataset.", "synthetic": syn}
        res = weather_mod.fetch_weekly_weather(c.lat, c.lon, start, as_of + timedelta(days=6), timeout=self.settings.open_meteo_timeout)
        res["synthetic"] = syn
        res["note"] = "Real observations are shown for comparison only; the demo forecast stays on synthetic weather so history and weather remain consistent."
        return res
