"""Thin client for NREL's PVWatts v8 API (https://developer.nrel.gov/docs/solar/pvwatts/v8/)
-- a plain HTTP GET, so no PVWatts-specific SDK is needed, just `requests`.

Reads the API key from the NREL_API_KEY env var, falling back to NREL's public,
rate-limited DEMO_KEY so this works out of the box for testing; set NREL_API_KEY to a
real key (https://developer.nrel.gov/signup/) for real use.
"""

from __future__ import annotations

import os

import numpy as np
import requests

from der_opt.time_grid import TimeGrid

PVWATTS_V8_URL = "https://developer.nrel.gov/api/pvwatts/v8.json"
DEMO_KEY = "DEMO_KEY"


def fetch_pvwatts_hourly_ac_kw(
    lat: float,
    lon: float,
    system_capacity_kw: float,
    azimuth: float = 180.0,
    tilt: float | None = None,
    array_type: int = 1,
    module_type: int = 0,
    losses: float = 14.0,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> np.ndarray:
    """Calls PVWatts v8 and returns one year of hourly AC output (8760 values, kW).
    `tilt` defaults to the site's latitude (a common simple rule of thumb) if not
    given. `array_type`: 0=fixed open rack, 1=fixed roof mount, 2=1-axis tracking,
    3=1-axis backtracked, 4=2-axis tracking. `module_type`: 0=standard, 1=premium,
    2=thin film."""
    resolved_key = api_key or os.environ.get("NREL_API_KEY", DEMO_KEY)
    resolved_tilt = abs(lat) if tilt is None else tilt

    params = {
        "api_key": resolved_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": system_capacity_kw,
        "azimuth": azimuth,
        "tilt": resolved_tilt,
        "array_type": array_type,
        "module_type": module_type,
        "losses": losses,
        "timeframe": "hourly",
    }
    response = requests.get(PVWATTS_V8_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()

    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"PVWatts API returned errors: {errors}")

    ac_watts = payload["outputs"]["ac"]
    return np.array(ac_watts, dtype=float) / 1000.0


def solar_availability_kw(time_grid: TimeGrid, hourly_ac_kw: np.ndarray, start_hour: int = 0) -> np.ndarray:
    """Slices a full year of PVWatts hourly AC output (from fetch_pvwatts_hourly_ac_kw,
    8760 values starting at Jan 1 hour 0) to a TimeGrid's horizon -- directly usable as
    SolarPV's `available_kw`. Hourly resolution only."""
    if time_grid.resolution_minutes != 60:
        raise ValueError("PVWatts output is hourly; use a TimeGrid with resolution_minutes=60")
    end = start_hour + time_grid.horizon_steps
    if start_hour < 0 or end > len(hourly_ac_kw):
        raise ValueError(
            f"Requested hour range [{start_hour}, {end}) exceeds the "
            f"{len(hourly_ac_kw)}-hour PVWatts profile"
        )
    return hourly_ac_kw[start_hour:end]
