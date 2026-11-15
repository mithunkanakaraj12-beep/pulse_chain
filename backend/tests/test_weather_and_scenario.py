from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from pulsechain.services import scenario as scen
from pulsechain.services import weather


def _daily(start: date, n: int):
    days = [(start.toordinal() + i) for i in range(n)]
    return {"daily": {"time": [date.fromordinal(d).isoformat() for d in days],
                      "precipitation_sum": [10.0] * n, "temperature_2m_mean": [28.0] * n}}


def test_open_meteo_weekly_aggregation_and_partial_week_dropped():
    sess = MagicMock()
    resp = MagicMock(); resp.json.return_value = _daily(date(2026, 5, 18), 17)  # 2 full weeks + 3 days
    resp.raise_for_status.return_value = None
    sess.get.return_value = resp
    out = weather.fetch_weekly_weather(20.0, 84.0, date(2026, 5, 18), date(2026, 6, 3), session=sess, today=date(2026, 11, 13))
    assert out["available"] and len(out["weeks"]) == 2 and out["weeks"][0]["rain_mm"] == 70.0
    assert sess.get.call_args[0][0] == weather.ARCHIVE_URL


def test_open_meteo_failure_falls_back_gracefully():
    sess = MagicMock(); sess.get.side_effect = requests.ConnectionError("offline")
    out = weather.fetch_weekly_weather(20.0, 84.0, date(2026, 5, 18), date(2026, 6, 3), session=sess, today=date(2026, 11, 13))
    assert out["available"] is False and "synthetic" in out["reason"]


def test_open_meteo_malformed_payload_falls_back():
    sess = MagicMock(); resp = MagicMock(); resp.json.return_value = {"nope": 1}; resp.raise_for_status.return_value = None
    sess.get.return_value = resp
    assert weather.fetch_weekly_weather(1, 1, date(2026, 5, 18), date(2026, 5, 24), session=sess, today=date(2026, 11, 13))["available"] is False


def _frame():
    return pd.DataFrame({"week_start": pd.date_range("2026-03-02", periods=12, freq="7D"), "cases": 5,
                         "rain_mm": 20.0, "temp_c": 30.0, "catchment_pop": 10000})


def test_overlay_changes_only_last_weeks_of_selected_regions_and_not_the_input():
    f = _frame()
    s = scen.validate({"monsoon": True, "intensity": 1.0, "regions": ["coastal"], "weeks": 3})
    out = scen.apply_overlay(f, "coastal", s)
    assert (out["rain_mm"].iloc[:-3] == 20.0).all() and (out["rain_mm"].iloc[-3:] > 100).all()
    assert (f["rain_mm"] == 20.0).all()  # original untouched
    assert scen.apply_overlay(f, "highland", s) is f  # unaffected region


def test_overlay_is_noop_for_baseline():
    f = _frame()
    assert scen.apply_overlay(f, "coastal", scen.validate({"monsoon": False})) is f


@pytest.mark.parametrize("bad", [{"intensity": 5}, {"intensity": "x"}, {"weeks": 0}, {"weeks": 9}, {"regions": ["mars"]}, {"regions": []}])
def test_scenario_validation_rejects_bad_input(bad):
    with pytest.raises(scen.ScenarioError):
        scen.validate({"monsoon": True, **bad})
