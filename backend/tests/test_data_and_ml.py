"""Dataset realism/reproducibility and forecasting-model quality checks (all on SIMULATED data)."""
import numpy as np
import pandas as pd
import pytest

from pulsechain import data_gen, db
from pulsechain.domain import DISEASE_IDS
from pulsechain.ml.inference import Forecaster


def test_dataset_scale_meets_requirements(settings):
    with db.session(settings.db_path) as conn:
        assert db.one(conn, "SELECT COUNT(*) n FROM clinics")["n"] >= 10
        assert db.one(conn, "SELECT COUNT(DISTINCT disease) n FROM cases")["n"] >= 3
        assert db.one(conn, "SELECT COUNT(DISTINCT medicine_id) n FROM consumption")["n"] >= 5
        assert db.one(conn, "SELECT COUNT(*) n FROM weather")["n"] > 0
        assert db.one(conn, "SELECT COUNT(*) n FROM inventory_batches WHERE expiry_date <> ''")["n"] > 0


def test_generation_is_reproducible_for_a_seed(tmp_path):
    a, b, c = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "c.db"
    data_gen.reset_demo(a, seed=7); data_gen.reset_demo(b, seed=7); data_gen.reset_demo(c, seed=8)
    q = "SELECT clinic_id, disease, week_start, cases FROM cases ORDER BY 1, 2, 3"
    with db.session(a) as ca, db.session(b) as cb, db.session(c) as cc:
        ra, rb, rc = db.rows(ca, q), db.rows(cb, q), db.rows(cc, q)
    assert ra == rb and ra != rc


def test_data_is_labelled_simulated(settings):
    with db.session(settings.db_path) as conn:
        label = str(db.get_meta(conn, "data_label", ""))
    assert "SIMULATED" in label.upper()


def test_cases_and_consumption_are_non_negative_integers(settings):
    with db.session(settings.db_path) as conn:
        assert db.one(conn, "SELECT MIN(cases) m FROM cases")["m"] >= 0
        assert db.one(conn, "SELECT MIN(units) m FROM consumption")["m"] >= 0
        assert db.one(conn, "SELECT MIN(rain_mm) m FROM weather")["m"] >= 0


def test_rainfall_leads_malaria_by_a_few_weeks(settings):
    """The simulated transmission mechanism should be recoverable: rain at lag 2-6 weeks correlates with malaria."""
    with db.session(settings.db_path) as conn:
        w = db.frame(conn, "SELECT clinic_id, week_start, rain_mm FROM weather")
        c = db.frame(conn, "SELECT clinic_id, week_start, cases FROM cases WHERE disease='malaria'")
    df = w.merge(c, on=["clinic_id", "week_start"]).sort_values(["clinic_id", "week_start"])
    def corr(lag):
        d = df.copy(); d["rain_lag"] = d.groupby("clinic_id")["rain_mm"].shift(lag)
        return d[["rain_lag", "cases"]].dropna().corr().iloc[0, 1]
    assert max(corr(l) for l in (2, 3, 4, 5, 6)) > corr(0) and corr(4) > 0.15


def test_medicine_use_tracks_cases(settings):
    """Malaria treatment consumption should move with malaria cases (protocol-driven, not random)."""
    with db.session(settings.db_path) as conn:
        c = db.frame(conn, "SELECT clinic_id, week_start, cases FROM cases WHERE disease='malaria'")
        u = db.frame(conn, "SELECT clinic_id, week_start, units FROM consumption WHERE medicine_id='act'")
    m = c.merge(u, on=["clinic_id", "week_start"])
    assert m[["cases", "units"]].corr().iloc[0, 1] > 0.8


@pytest.fixture(scope="module")
def metrics(trained_env):
    return Forecaster.load(trained_env.model_dir).metrics


def test_model_beats_both_baselines_on_held_out_weeks(metrics):
    for d in DISEASE_IDS:
        m = metrics["diseases"][d]
        assert m["model"]["mae"] <= min(b["mae"] for b in m["baselines"].values()) * 1.02, d
    # aggregate across diseases the model is clearly better than the best baseline
    assert sum(metrics["diseases"][d]["mae_improvement_vs_best_baseline_pct"] for d in DISEASE_IDS) > 0


def test_interval_coverage_is_reasonable(metrics):
    for d in DISEASE_IDS:
        cov = metrics["diseases"][d]["interval"]["empirical_coverage_after_cqr"]
        assert 0.70 <= cov <= 0.97, (d, cov)


def test_metrics_document_split_and_horizons(metrics):
    for d in DISEASE_IDS:
        m = metrics["diseases"][d]
        assert set(m["rows"]) == {"train", "calibration", "test"}
        assert sorted(int(h) for h in m["by_horizon"]) == list(range(1, 9))
        assert m["heavy_rain_subset"]["rows"] > 0


def _frame(settings, clinic="C01", disease="malaria"):
    from pulsechain.ml.train import load_clinic_frames
    with db.session(settings.db_path) as conn:
        return load_clinic_frames(conn, disease)[clinic]


def test_forecast_shape_bands_and_drivers(settings):
    fc = Forecaster.load(settings.model_dir)
    out = fc.forecast("malaria", _frame(settings), weeks=8)
    e, lo, hi = (np.array([w[k] for w in out["weeks"]], dtype=float) for k in ("expected", "lower", "upper"))
    assert len(e) == 8 and (lo <= e + 1e-6).all() and (e <= hi + 1e-6).all() and (lo >= 0).all()
    assert out["drivers"]["avg_weekly_cases_vs_typical"] and out["drivers"]["global_importance_pct"]
    assert all(w["baseline_persistence"] >= 0 and w["baseline_seasonal"] >= 0 for w in out["weeks"])


def test_forecast_is_deterministic(settings):
    fc = Forecaster.load(settings.model_dir)
    a = fc.forecast("cholera", _frame(settings, "C03", "cholera"), weeks=6, explain=False)
    b = fc.forecast("cholera", _frame(settings, "C03", "cholera"), weeks=6, explain=False)
    assert a["weeks"] == b["weeks"]


def test_more_rain_raises_malaria_forecast(settings):
    fc = Forecaster.load(settings.model_dir)
    base = _frame(settings, "C05")
    wet = base.copy(); wet.loc[wet.index[-3:], "rain_mm"] = wet["rain_mm"].iloc[-3:] + 250
    e0 = sum(w["expected"] for w in fc.forecast("malaria", base, 8, explain=False)["weeks"])
    e1 = sum(w["expected"] for w in fc.forecast("malaria", wet, 8, explain=False)["weeks"])
    assert e1 > e0


def test_forecast_without_model_raises(tmp_path):
    from pulsechain.ml.inference import ModelNotTrainedError
    with pytest.raises(ModelNotTrainedError):
        Forecaster.load(tmp_path / "nothing")
