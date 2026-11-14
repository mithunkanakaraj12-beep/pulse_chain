"""Feature engineering for the disease forecasting models.

The same function builds features for training (every historical week as a forecast origin) and for
inference (only the most recent week), so there is no train/serve skew.

Design: ONE pooled "direct multi-horizon" model per disease. Each training row is
(clinic, forecast origin week t, horizon h in 1..8) and the target is the log1p case rate per 1,000
catchment population at week t+h. Horizon is a feature, so a single model serves all horizons and
clinics of different sizes share statistical strength.

Only information available at the origin week is used (lagged cases, recent rainfall/temperature,
calendar position of the target week). Future weather is *not* assumed known; its uncertainty is
absorbed by the quantile prediction intervals.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_HORIZON = 8
MIN_HISTORY_WEEKS = 9  # longest look-back (rain 4-7 weeks ago) needs 8 prior rows + origin

FEATURE_GROUPS: dict[str, list[str]] = {
    "Recent case momentum": ["rate0", "rate_l1", "rate_l2", "rate_l3", "rate_m4", "rate_m8"],
    "Rainfall (recent & lagged)": ["rain0", "rain_m2", "rain_m4", "rain_m8", "rain_max4", "rain_m4_lag4"],
    "Temperature": ["temp0", "temp_m4"],
    "Seasonality (time of year)": ["woy_sin", "woy_cos"],
    "Catchment size": ["log_pop"],
    "Forecast horizon": ["h"],
}
FEATURE_COLUMNS: list[str] = [c for cols in FEATURE_GROUPS.values() for c in cols]
# Groups that can be meaningfully "ablated" to explain a single forecast.
EXPLAINABLE_GROUPS = ["Recent case momentum", "Rainfall (recent & lagged)", "Temperature"]


def origin_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-origin features. `frame` is one clinic's weekly series sorted by week_start with columns
    week_start (datetime64), cases, rain_mm, temp_c, catchment_pop."""
    pop_k = frame["catchment_pop"] / 1000.0
    rate = frame["cases"] / pop_k
    rain = frame["rain_mm"]
    temp = frame["temp_c"]
    out = pd.DataFrame(index=frame.index)
    out["week_start"] = frame["week_start"]
    out["rate0"] = np.log1p(rate)
    for lag in (1, 2, 3):
        out[f"rate_l{lag}"] = np.log1p(rate.shift(lag))
    out["rate_m4"] = np.log1p(rate.rolling(4).mean())
    out["rate_m8"] = np.log1p(rate.rolling(8).mean())
    out["rain0"] = rain
    out["rain_m2"] = rain.rolling(2).mean()
    out["rain_m4"] = rain.rolling(4).mean()
    out["rain_m8"] = rain.rolling(8).mean()
    out["rain_max4"] = rain.rolling(4).max()
    out["rain_m4_lag4"] = rain.rolling(4).mean().shift(4)
    out["temp0"] = temp
    out["temp_m4"] = temp.rolling(4).mean()
    out["log_pop"] = np.log(frame["catchment_pop"])
    return out


def stack_horizons(origin: pd.DataFrame, horizons: range | list[int]) -> pd.DataFrame:
    """Cross each origin row with each horizon and add target-week calendar features."""
    parts = []
    for h in horizons:
        part = origin.copy()
        part["h"] = h
        target_week = part["week_start"] + pd.to_timedelta(7 * h, unit="D")
        doy = target_week.dt.dayofyear.to_numpy(dtype=float)
        part["woy_sin"] = np.sin(2 * np.pi * doy / 365.25)
        part["woy_cos"] = np.cos(2 * np.pi * doy / 365.25)
        part["target_week"] = target_week
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def build_training_frame(clinic_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Stacked (clinic, origin, horizon) rows with target `y` (log1p rate at t+h) and actual cases."""
    rows = []
    for clinic_id, frame in clinic_frames.items():
        frame = frame.sort_values("week_start").reset_index(drop=True)
        origin = origin_features(frame)
        origin["clinic_id"] = clinic_id
        origin["catchment_pop"] = frame["catchment_pop"]
        origin["idx"] = np.arange(len(frame))
        rate = (frame["cases"] / (frame["catchment_pop"] / 1000.0)).to_numpy()
        cases = frame["cases"].to_numpy()
        stacked = stack_horizons(origin, range(1, MAX_HORIZON + 1))
        tgt_idx = stacked["idx"].to_numpy() + stacked["h"].to_numpy()
        valid = tgt_idx < len(frame)
        stacked = stacked.loc[valid].copy()
        tgt_idx = tgt_idx[valid]
        stacked["y"] = np.log1p(rate[tgt_idx])
        stacked["y_cases"] = cases[tgt_idx]
        rows.append(stacked)
    data = pd.concat(rows, ignore_index=True)
    return data.dropna(subset=FEATURE_COLUMNS + ["y"]).reset_index(drop=True)


def inference_frame(frame: pd.DataFrame, horizons: int = MAX_HORIZON) -> pd.DataFrame:
    """Features for the last observed week of `frame`, one row per horizon 1..horizons."""
    if len(frame) < MIN_HISTORY_WEEKS:
        raise ValueError(f"need at least {MIN_HISTORY_WEEKS} weeks of history, got {len(frame)}")
    frame = frame.sort_values("week_start").reset_index(drop=True)
    origin = origin_features(frame).iloc[[-1]]
    stacked = stack_horizons(origin, range(1, horizons + 1))
    if stacked[FEATURE_COLUMNS].isna().any().any():
        raise ValueError("insufficient history to compute all features")
    return stacked
