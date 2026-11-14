"""Reproducible training pipeline.

    python -m pulsechain.cli train            # trains from the SQLite demo database

Method
------
* Model: scikit-learn HistGradientBoostingRegressor (gradient-boosted trees). XGBoost would be a
  drop-in alternative; scikit-learn is used so the project installs with no compiled extras.
* One pooled model-set per disease: (a) a Poisson-loss model for the EXPECTED weekly case rate per
  1,000 population (expected value, not median, because demand planning needs the mean and the median
  collapses to zero for sparse counts), plus (b) 10th/90th percentile quantile models on log1p(rate)
  that form an 80% prediction interval.
* Chronological split (never shuffled): TRAIN | CALIBRATION (52 wks) | TEST (52 wks), with an
  8-week purge before each boundary so no training target overlaps a later window.
* Conformalised quantile regression (CQR): the calibration window is used to widen/narrow the raw
  quantile band so that it covers ~80% of outcomes; coverage is then *measured* on the untouched test window.
* Baselines scored on the same test rows: (1) persistence = mean of the last 4 weeks,
  (2) seasonal climatology = training-period mean for the same week of year.
* Explainability: grouped permutation importance on the test window (global) and a seasonally-matched
  ablation for individual forecasts (see inference.py).
* The shipped model is refit on ALL data using the calibrated band offset. Reported metrics are from the
  held-out test window of the earlier fit and are the honest generalisation estimate.

All metrics describe performance on SIMULATED data. They say nothing about real-world accuracy.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor

from .. import db
from ..domain import DISEASE_IDS
from .features import FEATURE_COLUMNS, FEATURE_GROUPS, MAX_HORIZON, build_training_frame

TARGET_COVERAGE = 0.8
HOLDOUT_WEEKS = 52
PURGE_WEEKS = MAX_HORIZON
MODEL_FILE = "forecast_models.joblib"
METRICS_FILE = "metrics.json"
MODEL_FORMAT_VERSION = 1


def load_clinic_frames(conn, disease: str) -> dict[str, pd.DataFrame]:
    cases = db.frame(conn, "SELECT clinic_id, week_start, cases FROM cases WHERE disease = ?", (disease,))
    weather = db.frame(conn, "SELECT clinic_id, week_start, rain_mm, temp_c FROM weather")
    clinics = db.frame(conn, "SELECT id AS clinic_id, catchment_pop FROM clinics")
    merged = cases.merge(weather, on=["clinic_id", "week_start"]).merge(clinics, on="clinic_id")
    merged["week_start"] = pd.to_datetime(merged["week_start"])
    return {cid: g.drop(columns="clinic_id").sort_values("week_start").reset_index(drop=True)
            for cid, g in merged.groupby("clinic_id")}


def _new_model(kind: str, seed: int) -> HistGradientBoostingRegressor:
    common = dict(max_iter=220, learning_rate=0.06, max_depth=5, min_samples_leaf=40,
                  l2_regularization=1.0, random_state=seed)
    if kind == "mean":  # expected case rate (Poisson deviance) - what demand planning needs
        return HistGradientBoostingRegressor(loss="poisson", **common)
    q = {"q10": 0.1, "q90": 0.9}[kind]  # band edges on log1p(rate)
    return HistGradientBoostingRegressor(loss="quantile", quantile=q, **common)


def fit_models(x: pd.DataFrame, seed: int) -> dict[str, HistGradientBoostingRegressor]:
    feats = x[FEATURE_COLUMNS]
    log_rate = x["y"].to_numpy()
    rate = np.expm1(log_rate)
    return {
        "mean": _new_model("mean", seed).fit(feats, rate),
        "q10": _new_model("q10", seed).fit(feats, log_rate),
        "q90": _new_model("q90", seed).fit(feats, log_rate),
    }


def raw_band_log(models: dict, x: pd.DataFrame) -> np.ndarray:
    lo = models["q10"].predict(x[FEATURE_COLUMNS])
    hi = models["q90"].predict(x[FEATURE_COLUMNS])
    return np.column_stack([np.minimum(lo, hi), np.maximum(lo, hi)])


def predict_rate(models: dict, x: pd.DataFrame, cqr_offset: float = 0.0) -> np.ndarray:
    """(n, 3) array of [lower, expected, upper] case rate per 1,000 population.

    expected = Poisson-loss model; lower/upper = CQR-calibrated 10th/90th percentile models, widened if
    needed so that the band always contains the expected value."""
    mean = np.clip(models["mean"].predict(x[FEATURE_COLUMNS]), 0, None)
    band = raw_band_log(models, x)
    lo = np.clip(np.expm1(band[:, 0] - cqr_offset), 0, None)
    hi = np.clip(np.expm1(band[:, 1] + cqr_offset), 0, None)
    return np.column_stack([np.minimum(lo, mean), mean, np.maximum(hi, mean)])


def to_cases(rate: np.ndarray, catchment_pop: np.ndarray) -> np.ndarray:
    pop_k = np.asarray(catchment_pop, dtype=float) / 1000.0
    return rate * (pop_k[:, None] if rate.ndim == 2 else pop_k)


def cqr_offset(models: dict, calib: pd.DataFrame) -> float:
    band = raw_band_log(models, calib)
    y = calib["y"].to_numpy()
    scores = np.maximum(band[:, 0] - y, y - band[:, 1])
    n = len(scores)
    level = min(1.0, np.ceil((n + 1) * TARGET_COVERAGE) / n)
    return float(np.quantile(scores, level))


def seasonal_tables(train: pd.DataFrame) -> tuple[dict, dict]:
    """(a) per-week-of-year median of every origin feature (+-4 week window) for explanation references;
    (b) mean case rate per 1,000 by week of year (+-1 week) for the climatology baseline."""
    woy = train["week_start"].dt.isocalendar().week.astype(int).clip(upper=52).to_numpy()
    ref: dict[int, dict[str, float]] = {}
    clim: dict[int, float] = {}
    rate_true = np.expm1(train["y"].to_numpy())
    target_woy = train["target_week"].dt.isocalendar().week.astype(int).clip(upper=52).to_numpy()
    for w in range(1, 53):
        dist = np.minimum(np.abs(woy - w), 52 - np.abs(woy - w))
        mask = dist <= 4
        sub = train.loc[mask]
        ref[w] = ({c: float(sub[c].median()) for c in FEATURE_COLUMNS if c not in ("h", "woy_sin", "woy_cos")}
                  if len(sub) else {})
        tdist = np.minimum(np.abs(target_woy - w), 52 - np.abs(target_woy - w))
        tmask = tdist <= 1
        clim[w] = float(rate_true[tmask].mean()) if tmask.any() else float(rate_true.mean())
    return ref, clim


def baselines(test: pd.DataFrame, clim: dict) -> dict[str, np.ndarray]:
    """Baseline predictions in *cases* for each test row."""
    pop = test["catchment_pop"].to_numpy(dtype=float)
    persistence = np.expm1(test["rate_m4"].to_numpy()) * pop / 1000.0
    tw = test["target_week"].dt.isocalendar().week.astype(int).clip(upper=52).to_numpy()
    seasonal = np.array([clim[int(w)] for w in tw]) * pop / 1000.0
    return {"persistence_4wk_mean": persistence, "seasonal_climatology": seasonal}


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    err = y_pred - y_true
    return {"mae": float(np.mean(np.abs(err))), "rmse": float(np.sqrt(np.mean(err ** 2)))}


def grouped_permutation_importance(models, test: pd.DataFrame, cqr: float, seed: int, repeats: int = 3) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    sample = test.sample(n=min(6000, len(test)), random_state=seed).reset_index(drop=True)
    pop = sample["catchment_pop"].to_numpy(dtype=float)
    truth = sample["y_cases"].to_numpy(dtype=float)
    base_mae = np.mean(np.abs(to_cases(predict_rate(models, sample, cqr)[:, 1], pop) - truth))
    out: dict[str, float] = {}
    for group, cols in FEATURE_GROUPS.items():
        deltas = []
        for _ in range(repeats):
            shuffled = sample.copy()
            perm = rng.permutation(len(shuffled))
            shuffled[cols] = sample[cols].to_numpy()[perm]
            mae = np.mean(np.abs(to_cases(predict_rate(models, shuffled, cqr)[:, 1], pop) - truth))
            deltas.append(mae - base_mae)
        out[group] = float(np.mean(deltas))
    return out


def evaluate_disease(data: pd.DataFrame, seed: int) -> dict:
    """Fit on TRAIN, calibrate on CALIBRATION, report on TEST."""
    last_origin = data["week_start"].max()
    test_start = last_origin - pd.Timedelta(weeks=HOLDOUT_WEEKS - 1)
    calib_start = test_start - pd.Timedelta(weeks=HOLDOUT_WEEKS)
    train = data[data["target_week"] < calib_start]  # purge: no training target reaches the calibration window
    calib = data[(data["week_start"] >= calib_start) & (data["target_week"] < test_start)]
    test = data[data["week_start"] >= test_start]

    models = fit_models(train, seed)
    offset = cqr_offset(models, calib)
    pop = test["catchment_pop"].to_numpy(dtype=float)
    cases_pred = to_cases(predict_rate(models, test, offset), pop)
    truth = test["y_cases"].to_numpy(dtype=float)
    ref, clim = seasonal_tables(train)
    base = baselines(test, clim)

    model_m = _metrics(truth, cases_pred[:, 1])
    base_m = {name: _metrics(truth, p) for name, p in base.items()}
    best_base = min(base_m, key=lambda k: base_m[k]["mae"])
    by_h = {}
    for h in range(1, MAX_HORIZON + 1):
        mask = test["h"].to_numpy() == h
        by_h[h] = {
            "model_mae": float(np.mean(np.abs(cases_pred[mask, 1] - truth[mask]))),
            "persistence_mae": float(np.mean(np.abs(base["persistence_4wk_mean"][mask] - truth[mask]))),
            "climatology_mae": float(np.mean(np.abs(base["seasonal_climatology"][mask] - truth[mask]))),
        }
    inside = (truth >= np.floor(cases_pred[:, 0])) & (truth <= np.ceil(cases_pred[:, 2]))
    raw_cases = to_cases(predict_rate(models, test, 0.0), pop)
    raw_inside = (truth >= np.floor(raw_cases[:, 0])) & (truth <= np.ceil(raw_cases[:, 2]))

    # Heavy-rain subset: origins whose recent rainfall is >=1.5x the seasonal norm. A calendar-only
    # baseline cannot react to these weeks; a weather-aware model should.
    woy_o = test["week_start"].dt.isocalendar().week.astype(int).clip(upper=52).to_numpy()
    ref_rain = np.array([ref[int(w)].get("rain_m4", np.nan) for w in woy_o])
    heavy = (test["rain_m4"].to_numpy() >= 1.5 * np.maximum(ref_rain, 10.0)) & (test["rain_m4"].to_numpy() >= 60)
    heavy_rain = None
    if heavy.sum() >= 150:
        heavy_rain = {
            "rows": int(heavy.sum()),
            "model_mae": float(np.mean(np.abs(cases_pred[heavy, 1] - truth[heavy]))),
            "persistence_mae": float(np.mean(np.abs(base["persistence_4wk_mean"][heavy] - truth[heavy]))),
            "climatology_mae": float(np.mean(np.abs(base["seasonal_climatology"][heavy] - truth[heavy]))),
        }
    importance = grouped_permutation_importance(models, test, offset, seed)
    return {
        "rows": {"train": int(len(train)), "calibration": int(len(calib)), "test": int(len(test))},
        "test_window": [test_start.date().isoformat(), last_origin.date().isoformat()],
        "model": model_m,
        "baselines": base_m,
        "best_baseline": best_base,
        "mae_improvement_vs_best_baseline_pct": 100.0 * (1 - model_m["mae"] / base_m[best_base]["mae"]),
        "interval": {
            "nominal_coverage": TARGET_COVERAGE,
            "empirical_coverage_after_cqr": float(inside.mean()),
            "empirical_coverage_raw": float(raw_inside.mean()),
            "cqr_offset_log_units": offset,
            "mean_width_cases": float(np.mean(cases_pred[:, 2] - cases_pred[:, 0])),
        },
        "heavy_rain_subset": heavy_rain,
        "by_horizon": by_h,
        "global_importance_mae_increase": importance,
    }


def train_all(db_path: Path, model_dir: Path, seed: int = 42, verbose: bool = True) -> dict:
    t0 = time.time()
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    bundle: dict = {"format_version": MODEL_FORMAT_VERSION, "diseases": {}, "sklearn_version": sklearn.__version__}
    report: dict = {"diseases": {}}
    with db.session(db_path) as conn:
        as_of = db.get_meta(conn, "as_of")
        for disease in DISEASE_IDS:
            frames = load_clinic_frames(conn, disease)
            data = build_training_frame(frames)
            if verbose:
                print(f"[train] {disease}: {len(data):,} rows", flush=True)
            result = evaluate_disease(data, seed)
            # Final fit on all data, reusing the calibrated band offset.
            models = fit_models(data, seed)
            ref, clim = seasonal_tables(data)
            bundle["diseases"][disease] = {
                "models": models, "cqr_offset": result["interval"]["cqr_offset_log_units"],
                "seasonal_reference": ref, "climatology_rate": clim,
            }
            report["diseases"][disease] = result
            if verbose:
                m, b = result["model"], result["baselines"][result["best_baseline"]]
                print(f"[train]   test MAE model={m['mae']:.2f} best-baseline({result['best_baseline']})={b['mae']:.2f} "
                      f"improvement={result['mae_improvement_vs_best_baseline_pct']:.1f}% "
                      f"coverage={result['interval']['empirical_coverage_after_cqr']:.2f}", flush=True)
    report.update({
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_as_of": as_of, "seed": seed, "sklearn_version": sklearn.__version__,
        "data_is_simulated": True,
        "model_description": "HistGradientBoostingRegressor: Poisson expected-rate model + CQR-calibrated p10/p90 quantile band, per disease",
        "train_seconds": round(time.time() - t0, 1),
    })
    bundle["data_as_of"] = as_of
    joblib.dump(bundle, model_dir / MODEL_FILE)
    (model_dir / METRICS_FILE).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
