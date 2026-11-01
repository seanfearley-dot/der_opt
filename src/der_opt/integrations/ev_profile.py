"""Adapter for the user's ev-daily-charging-model project (a statistical model of BEV
daily driving distance and residential charging load, PSRC-within-driver + NHTS-
calibrated, Ploetz/Jakobsson/Sprei-style). Two artifacts from that project feed a
V2XVehicle:

- `model_params.json`: fitted model output, including a charging section per
  distance-distribution family ("weibull"/"lognormal") with charger_kw, battery_kwh,
  and mean/p95 24-hour charging-demand profiles (population-average, not one vehicle's
  actual day) -- useful for sizing a device and as a reference curve to compare an
  optimized dispatch against, not as a literal binary plug-in schedule.
- `excel_build/raw_arrivals.csv` / `raw_departures.csv`: the real empirical PSRC
  home-arrival-hour and departure-hour observations the model's plug-in timing is
  built from -- the right source for an actual connected[t] plug-in window.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.time_grid import TimeGrid


def load_model_params(model_params_path: str | Path) -> dict:
    return json.loads(Path(model_params_path).read_text())


def typical_arrival_departure_hours(
    arrivals_csv: str | Path, departures_csv: str | Path, percentile: float = 50.0
) -> tuple[int, int]:
    """(arrival_hour, departure_hour), each an hour-of-day in [0, 24), at the given
    percentile of the empirical PSRC observations this EV model was calibrated on.
    percentile=50 (median) is a reasonable default for a single representative vehicle;
    a lower arrival/departure percentile models an earlier-than-typical driver."""
    arrivals = pd.read_csv(arrivals_csv)["arrive"]
    departures = pd.read_csv(departures_csv)["depart"]
    arrival_hour = int(round(float(np.percentile(arrivals, percentile)))) % 24
    departure_hour = int(round(float(np.percentile(departures, percentile)))) % 24
    return arrival_hour, departure_hour


def connected_schedule(time_grid: TimeGrid, arrival_hour: int, departure_hour: int) -> np.ndarray:
    """0/1 connected[t] array for time_grid: away (0) from departure_hour to
    arrival_hour each day, plugged in (1) overnight otherwise -- the standard "away
    driving during the day, home overnight" pattern these arrival/departure
    observations describe. time_grid must have `start` set."""
    if time_grid.start is None:
        raise ValueError("TimeGrid.start is required to build a connected_schedule")
    hours_of_day = [ts.hour + ts.minute / 60 for ts in time_grid.timestamps()]
    if arrival_hour <= departure_hour:
        # e.g. arrive 2am, depart 8am: connected only within [arrival, departure).
        return np.array(
            [1.0 if arrival_hour <= h < departure_hour else 0.0 for h in hours_of_day]
        )
    # typical case: arrive in the evening, depart the next morning -> connected wraps
    # past midnight.
    return np.array(
        [1.0 if (h >= arrival_hour or h < departure_hour) else 0.0 for h in hours_of_day]
    )


def charging_device_params(model_params: dict, family: str = "weibull") -> dict:
    """capacity_kwh / p_max_charge_kw sized from this model's fitted output, plus the
    p50 daily charging energy need -- a reasonable soc_departure_min_frac starting
    point is daily_charge_kwh_p50 / capacity_kwh."""
    charging = model_params["charging"][family]
    daily_kwh_p50 = model_params["simulated_summary"][family]["daily_charge_kwh_p50"]
    capacity_kwh = charging["battery_kwh"]
    return {
        "capacity_kwh": capacity_kwh,
        "p_max_charge_kw": charging["charger_kw"],
        "p_max_discharge_kw": charging["charger_kw"],
        "daily_charge_kwh_p50": daily_kwh_p50,
        "soc_departure_min_frac": min(daily_kwh_p50 / capacity_kwh, 1.0),
    }


def reference_charging_profile_kw(
    model_params: dict, family: str = "weibull", percentile: str = "mean"
) -> np.ndarray:
    """This model's own 24-hour uncontrolled-charging demand curve (kW, population-
    average or p95), for comparing an optimized/V2X-managed dispatch against what the
    model itself expects from uncontrolled Level-2 home charging."""
    key = f"{percentile}_hourly_profile_kw"
    return np.array(model_params["charging"][family][key], dtype=float)
