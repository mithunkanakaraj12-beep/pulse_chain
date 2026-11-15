"""Simulated monsoon scenario: a weather overlay applied to the most recent observed weeks.

The overlay replaces the last `weeks` weeks of rainfall (and cools temperature slightly) for the selected
regions *in memory only* - the stored history is never modified. The forecasting model then reacts to that
weather through its learned rainfall features. The size of the resulting case increase is whatever the model
predicts; it is NOT a fixed multiplier and NOT a real epidemiological forecast.
"""
from __future__ import annotations

import pandas as pd

from ..domain import REGIONS

BASE_WEEKLY_RAIN_MM = 180.0  # intensity 1.0 = an intense early-monsoon spell (illustrative)
REGION_FACTOR = {"highland": 0.9, "riverine": 1.1, "coastal": 1.2}
BUILD_UP = [0.8, 1.0, 1.1, 1.0]  # rainfall ramps up over the spell
TEMP_DROP_C = 2.0

DEFAULT_SCENARIO = {"monsoon": False, "intensity": 1.0, "regions": list(REGIONS), "weeks": 3}


class ScenarioError(ValueError):
    pass


def validate(payload: dict) -> dict:
    """Validate and normalise user-supplied scenario parameters."""
    try:
        monsoon = bool(payload.get("monsoon", False))
        intensity = float(payload.get("intensity", 1.0))
        weeks = int(payload.get("weeks", 3))
    except (TypeError, ValueError) as exc:
        raise ScenarioError("intensity must be a number and weeks an integer") from exc
    if not 0.5 <= intensity <= 2.0:
        raise ScenarioError("intensity must be between 0.5 and 2.0")
    if not 1 <= weeks <= 4:
        raise ScenarioError("weeks must be between 1 and 4")
    regions = payload.get("regions", list(REGIONS))
    if not isinstance(regions, list) or not regions or any(r not in REGIONS for r in regions):
        raise ScenarioError(f"regions must be a non-empty subset of {list(REGIONS)}")
    return {"monsoon": monsoon, "intensity": round(intensity, 2), "regions": sorted(set(regions)), "weeks": weeks}


def apply_overlay(frame: pd.DataFrame, region: str, scenario: dict) -> pd.DataFrame:
    """Return a copy of one clinic's weekly frame with the monsoon spell applied to its last weeks."""
    if not scenario.get("monsoon") or region not in scenario.get("regions", []):
        return frame
    out = frame.sort_values("week_start").reset_index(drop=True).copy()
    weeks = min(int(scenario["weeks"]), len(out))
    for i in range(weeks):
        idx = len(out) - weeks + i
        mm = BASE_WEEKLY_RAIN_MM * float(scenario["intensity"]) * REGION_FACTOR[region] * BUILD_UP[i % len(BUILD_UP)]
        out.loc[idx, "rain_mm"] = max(float(out.loc[idx, "rain_mm"]), mm)
        out.loc[idx, "temp_c"] = float(out.loc[idx, "temp_c"]) - TEMP_DROP_C
    return out


def signature(scenario: dict) -> str:
    if not scenario.get("monsoon"):
        return "baseline"
    return f"monsoon:{scenario['intensity']}:{scenario['weeks']}:{','.join(scenario['regions'])}"
