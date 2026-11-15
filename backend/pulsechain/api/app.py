"""Flask application factory: REST API under /api plus the static PWA frontend.

The service layer (`Platform`) is framework-agnostic; this module only parses/validates HTTP input,
maps domain errors to JSON error responses, and serves static files.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import data_gen
from ..config import Settings, load_settings
from ..domain import DISEASE_IDS, FORECAST_MAX_WEEKS, REGIONS
from ..ml.inference import ModelNotTrainedError
from ..services import views
from ..services.platform import ConflictError, NotFoundError, Platform, ValidationError
from ..services.scenario import ScenarioError
from ..services.sms import SmsError

log = logging.getLogger("pulsechain")


def ensure_ready(settings: Settings) -> None:
    """Create the demo database and train the model on first run (idempotent)."""
    if not settings.db_path.is_file():
        print("[setup] generating synthetic demo database ...", flush=True)
        data_gen.reset_demo(settings.db_path, seed=settings.seed)
    model_file = settings.model_dir / "forecast_models.joblib"
    if not model_file.is_file():
        print("[setup] training forecasting models (about 30 seconds, first run only) ...", flush=True)
        from ..ml.train import train_all
        train_all(settings.db_path, settings.model_dir, seed=settings.seed)


def _err(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    pl = Platform(settings)
    app.extensions["pulsechain"] = pl

    # ------------------------------------------------------------------ errors
    @app.errorhandler(ValidationError)
    @app.errorhandler(ScenarioError)
    def _bad(e):
        return _err("validation_error", str(e), 400)

    @app.errorhandler(NotFoundError)
    def _nf(e):
        return _err("not_found", str(e), 404)

    @app.errorhandler(ConflictError)
    def _conflict(e):
        return _err("conflict", str(e), 409)

    @app.errorhandler(SmsError)
    def _sms(e):
        return _err("sms_error", str(e), e.status)

    @app.errorhandler(ModelNotTrainedError)
    def _model(e):
        return _err("model_not_ready", str(e), 503)

    @app.errorhandler(HTTPException)
    def _http(e: HTTPException):
        if request.path.startswith("/api/"):
            return _err(e.name.lower().replace(" ", "_"), e.description or e.name, e.code or 500)
        return e

    @app.errorhandler(Exception)
    def _unhandled(e):
        log.exception("unhandled error")
        return _err("internal_error", "Unexpected server error", 500)

    @app.after_request
    def _headers(resp: Response):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ API
    @app.get("/api/health")
    def health():
        """Liveness + readiness: database present and model loaded."""
        return jsonify({"status": "ok", "database": settings.db_path.is_file(), "model_ready": pl.model_ready()})

    @app.get("/api/meta")
    def meta():
        """Application metadata: data label, as-of date, scenario, SMS mode, regions, diseases, medicines."""
        return jsonify(pl.meta())

    @app.get("/api/dashboard")
    def dashboard():
        """KPIs and chart series for the main dashboard."""
        return jsonify(views.dashboard(pl))

    @app.get("/api/clinics")
    def clinics():
        """All clinics with their worst risk level and earliest projected stockout."""
        return jsonify({"clinics": views.clinics_list(pl)})

    @app.get("/api/clinics/<clinic_id>")
    def clinic(clinic_id: str):
        """One clinic: inventory, disease trends + forecasts, nearby clinics, alerts, transfers, recommended actions."""
        return jsonify(views.clinic_detail(pl, clinic_id))

    @app.get("/api/forecast")
    def forecast():
        """Disease forecast. Query: disease (malaria|cholera|pneumonia), region (all|highland|riverine|coastal),
        weeks (1-8), clinic_id (optional, overrides region)."""
        disease = request.args.get("disease", "malaria")
        region = request.args.get("region", "all")
        clinic_id = request.args.get("clinic_id") or None
        try:
            weeks = int(request.args.get("weeks", FORECAST_MAX_WEEKS))
        except ValueError:
            raise ValidationError("weeks must be an integer")
        return jsonify(views.forecast_view(pl, disease, region, weeks, clinic_id))

    @app.get("/api/model/metrics")
    def model_metrics():
        """Held-out evaluation of the forecasting models versus baselines (simulated data)."""
        return jsonify({d: views._metrics_summary(pl, d) for d in DISEASE_IDS} | {"data_label": pl.meta()["data_label"],
                        "model": pl.meta()["model"]})

    @app.get("/api/inventory")
    def inventory():
        """Per clinic x medicine projections, with batches. Query: clinic_id (optional)."""
        return jsonify({"rows": pl.inventory_rows(clinic_id=request.args.get("clinic_id") or None)})

    @app.patch("/api/inventory/batches/<int:batch_id>")
    def patch_batch(batch_id: int):
        """Edit a batch's quantity and/or expiry_date (demo inventory). Body: {quantity?, expiry_date?}"""
        return jsonify(pl.update_batch(batch_id, _json_body()))

    @app.post("/api/inventory/batches")
    def add_batch():
        """Receive a new batch. Body: {clinic_id, medicine_id, quantity, expiry_date}"""
        return jsonify(pl.add_batch(_json_body())), 201

    @app.put("/api/inventory/<clinic_id>/<medicine_id>/policy")
    def put_policy(clinic_id: str, medicine_id: str):
        """Set the minimum safety stock policy. Body: {min_safety_stock}"""
        return jsonify(pl.update_policy(clinic_id, medicine_id, _json_body()))

    @app.get("/api/redistribution")
    def redistribution():
        """Proposed transfers (recommendations needing human approval), decided transfers, and map points."""
        return jsonify(pl.list_transfers())

    @app.post("/api/redistribution/decision")
    def redistribution_decision():
        """Approve or reject one proposal. Body: {id, decision: 'approve'|'reject', note?}.
        Approval updates SIMULATED demo inventory only."""
        body = _json_body()
        decision = body.get("decision")
        if decision not in ("approve", "reject") or not isinstance(body.get("id"), str):
            raise ValidationError("Body must include id (string) and decision ('approve' or 'reject')")
        note = body.get("note") if isinstance(body.get("note"), str) else None
        return jsonify(pl.decide_transfer(body["id"], decision == "approve", note))

    @app.get("/api/alerts")
    def alerts():
        """All alerts (critical shortages, upcoming shortages, disease surges, replenishment) with SMS previews."""
        return jsonify(pl.alerts())

    @app.post("/api/alerts/acknowledge")
    def ack():
        """Acknowledge an alert. Body: {key}"""
        key = _json_body().get("key")
        if not isinstance(key, str):
            raise ValidationError("key (string) is required")
        return jsonify(pl.acknowledge(key))

    @app.post("/api/sms/send")
    def sms_send():
        """Send (simulate) the SMS for an alert. Body: {alert_key, mode?: 'simulated'|'live', confirm?: bool}.
        Defaults to simulated. Live delivery needs server-side opt-in AND confirm=true."""
        body = _json_body()
        key = body.get("alert_key")
        if not isinstance(key, str):
            raise ValidationError("alert_key (string) is required")
        mode = body.get("mode", "simulated")
        return jsonify(pl.send_sms(key, mode, bool(body.get("confirm", False)))), 201

    @app.get("/api/sms/log")
    def sms_log():
        """Recent SMS log (simulated and live)."""
        return jsonify({"messages": pl.sms_log()})

    @app.get("/api/settings")
    def get_settings():
        """Alert threshold and SMS configuration (secrets are never returned)."""
        m = pl.meta()
        return jsonify({"alert_threshold": m["alert_threshold"], "sms": m["sms"], "weather": m["weather"]})

    @app.put("/api/settings")
    def put_settings():
        """Update alert threshold. Body: {alert_threshold: 'moderate'|'high'|'critical'}"""
        level = _json_body().get("alert_threshold")
        return jsonify({"alert_threshold": pl.set_alert_threshold(level)})

    @app.get("/api/scenario")
    def get_scenario():
        """The currently applied scenario (baseline or simulated monsoon)."""
        with pl.session() as conn:
            return jsonify(pl.get_scenario(conn))

    @app.post("/api/scenario")
    def set_scenario():
        """Apply a scenario. Body: {monsoon: bool, intensity: 0.5-2.0, weeks: 1-4, regions: [..]}"""
        return jsonify(pl.set_scenario(_json_body()))

    @app.get("/api/scenario/walkthrough")
    def walkthrough():
        """Read-only baseline-vs-monsoon comparison powering the 8-step demo. Query: intensity, weeks, regions (comma list)."""
        try:
            intensity = float(request.args.get("intensity", 1.0))
            weeks = int(request.args.get("weeks", 3))
        except ValueError:
            raise ValidationError("intensity must be a number and weeks an integer")
        regions = [r for r in request.args.get("regions", "").split(",") if r] or list(REGIONS)
        return jsonify(views.walkthrough(pl, intensity, regions, weeks))

    @app.get("/api/weather/compare")
    def weather_compare():
        """Open-Meteo observations vs the synthetic weather for a clinic (falls back gracefully). Query: clinic_id"""
        return jsonify(pl.weather_compare(request.args.get("clinic_id", "")))

    @app.post("/api/admin/reset")
    def admin_reset():
        """Reset the demo: regenerate the synthetic database (same seed), clear decisions, SMS log and scenario."""
        data_gen.reset_demo(settings.db_path, seed=settings.seed)
        pl.clear_caches()
        return jsonify({"status": "reset", "scenario": {"monsoon": False}})

    @app.get("/api/docs")
    def docs():
        """Machine-readable route list generated from the code."""
        routes = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            if not rule.rule.startswith("/api/"):
                continue
            fn = app.view_functions[rule.endpoint]
            routes.append({"path": rule.rule, "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
                           "description": " ".join((fn.__doc__ or "").split())})
        return jsonify({"routes": routes})

    # ------------------------------------------------------------------ frontend (static PWA)
    frontend = Path(settings.frontend_dir)

    def _asset_files() -> list[str]:
        """Every shipped frontend file except the service worker itself (precached by the service worker)."""
        out = []
        for path in sorted(frontend.rglob("*")):
            if path.is_file() and path.name not in ("sw.js", ".DS_Store"):
                out.append("/" + path.relative_to(frontend).as_posix())
        return out

    def _build_id() -> str:
        digest = hashlib.sha256()
        for rel in _asset_files():
            digest.update(rel.encode())
            digest.update((frontend / rel.lstrip("/")).read_bytes())
        return digest.hexdigest()[:12]

    @app.get("/asset-manifest.json")
    def asset_manifest():
        """Files the service worker precaches for offline use, plus a content hash that changes when any of them change."""
        files = [f for f in _asset_files() if f != "/index.html"]
        resp = jsonify({"build": _build_id(), "files": files})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(frontend, "index.html")

    @app.get("/sw.js")
    def service_worker():
        """Service worker with the content hash injected so installed apps update when the frontend changes."""
        source = (frontend / "sw.js").read_text(encoding="utf-8").replace("__BUILD__", _build_id())
        resp = Response(source, mimetype="text/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/manifest.webmanifest")
    def web_manifest():
        resp = send_from_directory(frontend, "manifest.webmanifest", mimetype="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/<path:filename>")
    def static_files(filename: str):
        if filename.startswith("api/"):
            return _err("not_found", "Unknown API route", 404)
        resp = send_from_directory(frontend, filename)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    return app
