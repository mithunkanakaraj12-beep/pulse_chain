"""Open-Meteo weather connector (no API key required) with graceful fallback.

The app never *depends* on this: forecasts run on the synthetic weather stored in the database. This
connector shows how real observations plug in - `fetch_weekly_weather` returns weekly rainfall and mean
temperature in exactly the shape the feature pipeline consumes. If the network is unavailable or the feature is
disabled it returns `available=False` with a reason, and callers fall back to the synthetic data.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fetch_weekly_weather(lat: float, lon: float, start: date, end: date, *, timeout: float = 6.0,
                         session: requests.Session | None = None, today: date | None = None) -> dict:
    """Weekly (Monday-start) rainfall sum and mean temperature between start and end, inclusive."""
    http = session or requests
    today = today or date.today()
    url = ARCHIVE_URL if end < today - timedelta(days=5) else FORECAST_URL
    params = {
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "daily": "precipitation_sum,temperature_2m_mean", "timezone": "UTC",
    }
    if url == ARCHIVE_URL:
        params.update(start_date=start.isoformat(), end_date=end.isoformat())
    else:
        params.update(start_date=start.isoformat(), end_date=min(end, today + timedelta(days=15)).isoformat())
    try:
        resp = http.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        days = payload["daily"]["time"]
        rain = payload["daily"]["precipitation_sum"]
        temp = payload["daily"]["temperature_2m_mean"]
    except requests.RequestException as exc:
        return {"available": False, "reason": f"Open-Meteo unreachable ({exc.__class__.__name__}); using synthetic weather."}
    except (KeyError, ValueError, TypeError):
        return {"available": False, "reason": "Open-Meteo returned an unexpected response; using synthetic weather."}

    buckets: dict[date, dict[str, list[float]]] = defaultdict(lambda: {"rain": [], "temp": []})
    for d_str, r, t in zip(days, rain, temp):
        wk = _week_monday(date.fromisoformat(d_str))
        if r is not None:
            buckets[wk]["rain"].append(float(r))
        if t is not None:
            buckets[wk]["temp"].append(float(t))
    weeks = []
    for wk in sorted(buckets):
        b = buckets[wk]
        if len(b["rain"]) < 7:  # incomplete week - skip rather than under-report rainfall
            continue
        weeks.append({"week_start": wk.isoformat(), "rain_mm": round(sum(b["rain"]), 1),
                      "temp_c": round(sum(b["temp"]) / max(1, len(b["temp"])), 1)})
    if not weeks:
        return {"available": False, "reason": "Open-Meteo returned no complete weeks for that window."}
    return {"available": True, "source": "open-meteo", "weeks": weeks}
