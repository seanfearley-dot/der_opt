from datetime import datetime
from pathlib import Path

import pytest

from der_opt.integrations.ev_profile import (
    charging_device_params,
    connected_schedule,
    load_model_params,
    reference_charging_profile_kw,
    typical_arrival_departure_hours,
)
from der_opt.time_grid import TimeGrid

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ev_model_sample"


def test_typical_arrival_departure_hours_matches_median():
    arrival, departure = typical_arrival_departure_hours(
        FIXTURE_DIR / "raw_arrivals.csv", FIXTURE_DIR / "raw_departures.csv"
    )
    assert arrival == 17
    assert departure == 8


def test_connected_schedule_wraps_past_midnight():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 6, 1))
    connected = connected_schedule(tg, arrival_hour=17, departure_hour=8)
    expected = [1.0] * 8 + [0.0] * 9 + [1.0] * 7  # home 0-7, away 8-16, home 17-23
    assert connected.tolist() == expected


def test_connected_schedule_same_day_window():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 6, 1))
    connected = connected_schedule(tg, arrival_hour=2, departure_hour=6)
    expected = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0] + [0.0] * 18
    assert connected.tolist() == expected


def test_charging_device_params_from_model():
    params = load_model_params(FIXTURE_DIR / "model_params.json")
    device_params = charging_device_params(params, family="weibull")
    assert device_params["capacity_kwh"] == pytest.approx(75.0)
    assert device_params["p_max_charge_kw"] == pytest.approx(7.2)
    assert device_params["soc_departure_min_frac"] == pytest.approx(7.5 / 75.0)


def test_reference_charging_profile_has_24_hours():
    params = load_model_params(FIXTURE_DIR / "model_params.json")
    profile = reference_charging_profile_kw(params, family="weibull", percentile="mean")
    assert len(profile) == 24
    assert profile[17] == pytest.approx(5.0)
