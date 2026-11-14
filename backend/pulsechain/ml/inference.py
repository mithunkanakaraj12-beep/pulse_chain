"""Forecast inference and per-forecast explanation using the trained model bundle."""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .features import EXPLAINABLE_GROUPS, FEATURE_GROUPS, MAX_HORIZON, inference_frame
from .train import METRICS_FILE, MODEL_FILE, predict_rate, to_cases


class ModelNotTrainedError(RuntimeError):
    """Raised when no trained model bundle exists yet."""


class Forecaster:
    def __init__(self, bundle: dict, metrics: dict | None, model_path: Path):
        self._bundle = bundle
        self.metrics = metrics or {}
        self.model_path = model_path
        self.version = f"{bundle.get('data_as_of')}-{int(model_path.stat().st_mtime)}"

    @classmethod
    def load(cls, model_dir: Path) -> "Forecaster":
        model_path = Path(model_dir) / MODEL_FILE
        if not model_path.is_file():
            raise ModelNotTrainedError(
                f"No trained model at {model_path}. Run `python -m pulsechain.cli train` first.")
        bundle = joblib.load(model_path)
        metrics_path = Path(model_dir) / METRICS_FILE
        metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else None
        return cls(bundle, metrics, model_path)

    def forecast(self, disease: str, frame: pd.DataFrame, weeks: int = MAX_HORIZON, explain: bool = True) -> dict:
        """Forecast the next `weeks` weeks for one clinic.

        `frame`: weekly history for ONE clinic (week_start, cases, rain_mm, temp_c, catchment_pop),
        ending at the origin week. Returns expected cases and an 80% prediction interval per week,
        two baselines, and (optionally) a seasonally-matched explanation of the drivers.
        """
        if disease not in self._bundle["diseases"]:
            raise KeyError(f"unknown disease {disease!r}")
        if not 1 <= weeks <= MAX_HORIZON:
            raise ValueError(f"weeks must be between 1 and {MAX_HORIZON}")
        spec = self._bundle["diseases"][disease]
        x = inference_frame(frame, horizons=MAX_HORIZON)
        pop = np.full(len(x), float(frame["catchment_pop"].iloc[-1]))
        cases = to_cases(predict_rate(spec["models"], x, spec["cqr_offset"]), pop)

        recent = frame.sort_values("week_start")["cases"].to_numpy(dtype=float)
        persistence = float(np.mean(recent[-4:]))
        target_woy = x["target_week"].dt.isocalendar().week.astype(int).clip(upper=52).to_numpy()
        clim = np.array([spec["climatology_rate"][int(w)] for w in target_woy]) * pop / 1000.0

        origin_week = x["week_start"].iloc[0].date()
        out_weeks = []
        for i in range(weeks):
            out_weeks.append({
                "horizon": int(x["h"].iloc[i]),
                "week_start": x["target_week"].iloc[i].date().isoformat(),
                "lower": round(float(cases[i, 0]), 2),
                "expected": round(float(cases[i, 1]), 2),
                "upper": round(float(cases[i, 2]), 2),
                "baseline_persistence": round(persistence, 2),
                "baseline_seasonal": round(float(clim[i]), 2),
            })
        result = {"disease": disease, "origin_week": origin_week.isoformat(), "weeks": out_weeks}
        if explain:
            result["drivers"] = self._explain(spec, x, pop, weeks, cases[:, 1])
            result["drivers"]["global_importance_pct"] = self.global_importance_share(disease)
        return result

    @staticmethod
    def _explain(spec: dict, x: pd.DataFrame, pop: np.ndarray, weeks: int, expected: np.ndarray) -> dict:
        """Seasonally-matched ablation: replace a feature group by its typical value for this time of year
        and measure how the expected forecast changes. This is an approximation of marginal influence
        (not SHAP) and groups interact, so the parts need not sum exactly to the total."""
        origin_woy = int(x["week_start"].iloc[0].isocalendar().week)
        ref = spec["seasonal_reference"].get(min(origin_woy, 52)) or {}
        actual = expected[:weeks]

        def predict_with(groups: list[str]) -> np.ndarray:
            x2 = x.copy()
            for g in groups:
                for col in FEATURE_GROUPS[g]:
                    if col in ref:
                        x2[col] = ref[col]
            return to_cases(predict_rate(spec["models"], x2, 0.0)[:, 1], pop)[:weeks]

        per_group = {}
        for g in EXPLAINABLE_GROUPS:
            per_group[g] = float(np.mean(actual - predict_with([g])))
        typical = predict_with(EXPLAINABLE_GROUPS)
        return {
            "method": "Seasonally-matched ablation: change in expected weekly cases when this factor is set to "
                      "its typical value for the time of year. Positive = factor is pushing the forecast up.",
            "avg_weekly_cases_vs_typical": {g: round(v, 2) for g, v in per_group.items()},
            "expected_if_typical_conditions": [round(float(v), 2) for v in typical],
            "total_vs_typical_avg_weekly": round(float(np.mean(actual - typical)), 2),
        }

    def global_importance_share(self, disease: str) -> dict[str, float]:
        """Held-out grouped permutation importance, as % of total (from the training report)."""
        raw = (self.metrics.get("diseases", {}).get(disease, {}).get("global_importance_mae_increase") or {})
        pos = {k: max(0.0, v) for k, v in raw.items()}
        total = sum(pos.values())
        return {k: round(100.0 * v / total, 1) for k, v in pos.items()} if total > 0 else {}
