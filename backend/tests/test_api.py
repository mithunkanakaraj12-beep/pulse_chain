"""HTTP-level tests: contract, validation, the monsoon scenario, approvals, SMS safety and invariants."""
import pytest

GET_ROUTES = ["/api/health", "/api/meta", "/api/dashboard", "/api/clinics", "/api/clinics/C01",
              "/api/forecast?disease=malaria", "/api/forecast?disease=cholera&region=coastal&weeks=4",
              "/api/model/metrics", "/api/inventory", "/api/redistribution", "/api/alerts", "/api/sms/log",
              "/api/settings", "/api/scenario", "/api/scenario/walkthrough", "/api/docs"]


@pytest.mark.parametrize("url", GET_ROUTES)
def test_get_routes_return_200_json(client, url):
    r = client.get(url)
    assert r.status_code == 200 and r.is_json


def test_data_is_labelled_simulated_everywhere(client):
    for url in ("/api/meta", "/api/dashboard", "/api/forecast?disease=malaria", "/api/model/metrics"):
        assert "SIMULATED" in client.get(url).get_json()["data_label"]


def test_errors_are_json_with_proper_status(client):
    assert client.get("/api/clinics/NOPE").status_code == 404
    assert client.get("/api/nothing").status_code == 404
    assert client.get("/api/nothing").is_json
    assert client.get("/api/forecast?disease=plague").status_code == 400
    assert client.get("/api/forecast?weeks=abc").status_code == 400
    assert client.get("/api/forecast?weeks=99").status_code == 400
    r = client.post("/api/scenario", data="not json", content_type="application/json")
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "validation_error"


def test_security_headers_and_no_store_on_api(client):
    r = client.get("/api/health")
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"


def test_docs_lists_routes(client):
    paths = {r["path"] for r in client.get("/api/docs").get_json()["routes"]}
    assert {"/api/forecast", "/api/inventory", "/api/redistribution/decision", "/api/sms/send"} <= paths


def test_forecast_has_bands_baselines_and_drivers(client):
    d = client.get("/api/forecast?disease=malaria&region=all").get_json()
    f = d["forecast"]
    assert d["disease"] == "malaria" and d["clinic_count"] == 12 and len(d["forecast_dates"]) == 8
    assert all(l <= e <= u for l, e, u in zip(f["lower"], f["expected"], f["upper"]))
    assert len(f["baseline_persistence"]) == 8 and len(f["baseline_seasonal"]) == 8
    assert d["drivers"]["avg_weekly_cases_vs_typical"] and d["demand"] and d["model_evaluation"]["baseline_mae"]


# ---------------------------------------------------------------- scenario
def _monsoon(client, **kw):
    body = {"monsoon": True, "intensity": 1.0, "weeks": 3} | kw
    r = client.post("/api/scenario", json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_monsoon_creates_more_alerts_and_serious_ones(client):
    before = client.get("/api/alerts").get_json()
    _monsoon(client)
    after = client.get("/api/alerts").get_json()
    assert after["active"] > before["active"]
    assert any(a["severity"] in ("critical", "high") for a in after["alerts"])


def test_monsoon_raises_malaria_forecast_and_can_be_reverted(client):
    total = lambda: sum(client.get("/api/forecast?disease=malaria").get_json()["forecast"]["expected"])
    t0 = total()
    _monsoon(client)
    assert total() > t0 * 1.2
    client.post("/api/scenario", json={"monsoon": False})
    assert total() == t0


def test_scenario_validation(client):
    assert client.post("/api/scenario", json={"monsoon": True, "intensity": 9}).status_code == 400
    assert client.post("/api/scenario", json={"monsoon": True, "weeks": 0}).status_code == 400
    assert client.post("/api/scenario", json={"monsoon": True, "regions": ["mars"]}).status_code == 400


def test_walkthrough_has_eight_steps_disclaimer_and_is_read_only(client):
    before = client.get("/api/scenario").get_json()
    d = client.get("/api/scenario/walkthrough").get_json()
    assert sorted(d["steps"]) == sorted(["normal", "monsoon", "forecast", "inventory_impact", "shortages", "sms", "redistribution", "final"])
    assert "SIMULATED" in d["disclaimer"] and "NOT a scientifically established" in d["disclaimer"]
    assert client.get("/api/scenario").get_json() == before  # walking through never applies the scenario
    assert client.get("/api/sms/log").get_json()["messages"] == []  # and never sends or logs anything


def test_walkthrough_monsoon_increases_risk(client):
    f = client.get("/api/scenario/walkthrough").get_json()["steps"]["final"]
    assert f["at_risk_with_monsoon"] > f["at_risk_before_monsoon"]


# ---------------------------------------------------------------- inventory
def _row(client, clinic="C01", med="act"):
    rows = client.get(f"/api/inventory?clinic_id={clinic}").get_json()["rows"]
    return next(r for r in rows if r["medicine_id"] == med)


def test_editing_a_batch_recalculates_projection(client):
    row = _row(client)
    bid = row["batches"][0]["id"]
    r = client.patch(f"/api/inventory/batches/{bid}", json={"quantity": 0})
    assert r.status_code == 200
    after = _row(client)
    assert after["days_to_stockout"] < row["days_to_stockout"]


def test_inventory_edit_validation(client):
    bid = _row(client)["batches"][0]["id"]
    assert client.patch(f"/api/inventory/batches/{bid}", json={"quantity": -5}).status_code == 400
    assert client.patch(f"/api/inventory/batches/{bid}", json={"quantity": "lots"}).status_code == 400
    assert client.patch(f"/api/inventory/batches/{bid}", json={"expiry_date": "yesterday"}).status_code == 400
    assert client.patch("/api/inventory/batches/999999", json={"quantity": 1}).status_code == 404


def test_receiving_a_batch_extends_cover(client):
    row = _row(client)
    r = client.post("/api/inventory/batches", json={"clinic_id": "C01", "medicine_id": "act", "quantity": 500, "expiry_date": "2027-12-31"})
    assert r.status_code == 201
    after = _row(client)["days_to_stockout"]
    assert after is None or after > row["days_to_stockout"]  # None = no stockout within the projection horizon
    assert client.post("/api/inventory/batches", json={"clinic_id": "C01", "medicine_id": "nope", "quantity": 5, "expiry_date": "2027-12-31"}).status_code in (400, 404)


def test_policy_update_validation(client):
    assert client.put("/api/inventory/C01/act/policy", json={"min_safety_stock": 25}).status_code == 200
    assert client.put("/api/inventory/C01/act/policy", json={"min_safety_stock": -1}).status_code == 400


# ---------------------------------------------------------------- redistribution
def test_transfers_require_human_approval_and_are_never_applied_automatically(client):
    d = client.get("/api/redistribution").get_json()
    assert d["proposed"] and all(p["requires_human_approval"] for p in d["proposed"])
    assert d["decided"] == []
    assert "Recommendations only" in d["notice"]


def test_approving_a_transfer_moves_simulated_stock(client):
    p = client.get("/api/redistribution").get_json()["proposed"][0]
    donor_before = _row(client, p["donor_id"], p["medicine_id"])
    r = client.post("/api/redistribution/decision", json={"id": p["id"], "decision": "approve"})
    assert r.status_code == 200
    donor_after = _row(client, p["donor_id"], p["medicine_id"])
    assert sum(b["quantity"] for b in donor_after["batches"]) < sum(b["quantity"] for b in donor_before["batches"])
    d = client.get("/api/redistribution").get_json()
    assert any(x["proposal_key"] == p["id"] and x["status"] == "approved" for x in d["decided"]) and all(x["id"] != p["id"] for x in d["proposed"])
    # deciding the same proposal again is a conflict, not a second transfer
    assert client.post("/api/redistribution/decision", json={"id": p["id"], "decision": "approve"}).status_code in (404, 409)


def test_rejecting_a_transfer_leaves_stock_untouched(client):
    p = client.get("/api/redistribution").get_json()["proposed"][0]
    before = _row(client, p["donor_id"], p["medicine_id"])
    assert client.post("/api/redistribution/decision", json={"id": p["id"], "decision": "reject", "note": "no vehicle"}).status_code == 200
    assert _row(client, p["donor_id"], p["medicine_id"])["batches"] == before["batches"]


def test_decision_validation_and_unknown_id(client):
    assert client.post("/api/redistribution/decision", json={"id": "x", "decision": "maybe"}).status_code == 400
    assert client.post("/api/redistribution/decision", json={"id": "act:C01->C02", "decision": "approve"}).status_code in (404, 409)


def test_expired_stock_is_never_proposed_for_transfer(client):
    props = client.get("/api/redistribution").get_json()["proposed"]
    expired_ids = {b["id"] for r in client.get("/api/inventory").get_json()["rows"] for b in r["batches"] if b["expired"]}
    assert expired_ids
    for p in props:
        assert not ({b["batch_id"] for b in p["batches"]} & expired_ids)


# ---------------------------------------------------------------- alerts + SMS
def _first_alert(client):
    return client.get("/api/alerts").get_json()["alerts"][0]


def test_sms_defaults_to_simulated_and_is_logged(client):
    a = _first_alert(client)
    r = client.post("/api/sms/send", json={"alert_key": a["key"]})
    assert r.status_code == 201 and r.get_json()["mode"] == "simulated"
    log = client.get("/api/sms/log").get_json()["messages"]
    assert len(log) == 1 and log[0]["status"] == "simulated" and len(log[0]["body"]) <= 160


def test_live_sms_is_refused_when_not_enabled_on_server(client):
    a = _first_alert(client)
    r = client.post("/api/sms/send", json={"alert_key": a["key"], "mode": "live", "confirm": True})
    assert r.status_code == 403
    assert all(m["status"] != "sent" for m in client.get("/api/sms/log").get_json()["messages"])


def test_sms_unknown_alert_and_missing_key(client):
    assert client.post("/api/sms/send", json={}).status_code == 400
    assert client.post("/api/sms/send", json={"alert_key": "nope"}).status_code == 404


def test_acknowledge_alert(client):
    a = _first_alert(client)
    assert client.post("/api/alerts/acknowledge", json={"key": a["key"]}).status_code == 200
    got = next(x for x in client.get("/api/alerts").get_json()["alerts"] if x["key"] == a["key"])
    assert got["acknowledged"] is True
    assert client.post("/api/alerts/acknowledge", json={}).status_code == 400


def test_alert_threshold_setting_changes_sms_eligibility(client):
    assert client.put("/api/settings", json={"alert_threshold": "banana"}).status_code == 400
    assert client.put("/api/settings", json={"alert_threshold": "critical"}).status_code == 200
    assert client.get("/api/settings").get_json()["alert_threshold"] == "critical"


def test_settings_never_expose_secrets(client):
    text = client.get("/api/settings").get_data(as_text=True).lower()
    assert "auth_token" not in text and "twilio_auth" not in text


# ---------------------------------------------------------------- admin + invariants
def test_admin_reset_restores_baseline(client):
    _monsoon(client)
    p = client.get("/api/redistribution").get_json()["proposed"]
    if p:
        client.post("/api/redistribution/decision", json={"id": p[0]["id"], "decision": "approve"})
    assert client.post("/api/admin/reset").status_code == 200
    assert client.get("/api/scenario").get_json()["monsoon"] is False
    assert client.get("/api/redistribution").get_json()["decided"] == []


@pytest.mark.parametrize("intensity", [0.5, 1.0, 2.0])
def test_safety_invariants_hold_for_every_monsoon_intensity(client, intensity):
    _monsoon(client, intensity=intensity)
    inv = {(r["clinic_id"], r["medicine_id"]): r for r in client.get("/api/inventory").get_json()["rows"]}
    batches = {b["id"]: b for r in inv.values() for b in r["batches"]}
    props = client.get("/api/redistribution").get_json()["proposed"]
    for p in props:
        assert p["requires_human_approval"] and p["donor_id"] != p["receiver_id"] and p["quantity"] > 0
        stockout = inv[(p["receiver_id"], p["medicine_id"])].get("stockout_date")
        if stockout:
            assert p["arrival_date"] < stockout
        assert p["donor_stock_after_cover_period"] >= p["donor_min_safety_stock"]
        for b in p["batches"]:
            src = batches[b["batch_id"]]
            assert not src["expired"] and b["quantity"] <= src["quantity"] and b["expiry_date"] > p["arrival_date"]
    # SMS lengths always fit one segment
    for a in client.get("/api/alerts").get_json()["alerts"]:
        if a.get("sms_preview"):
            assert len(a["sms_preview"]) <= 160 and a["sms_preview"].isascii()
