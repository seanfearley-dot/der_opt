"""EV daily patterns (docs/EV_PATTERN_SPEC.md v1.0)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pyomo.environ as pyo
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.devices import V2XVehicle
from der_opt.devices.ev_pattern import (
    expand_pattern,
    parse_pattern,
    pattern_device_params,
)
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing.sources import ConstantPriceSeries
from der_opt.scenarios import load_scenario
from der_opt.time_grid import TimeGrid

REPO = Path(__file__).parent.parent.parent
runner = CliRunner()
COMMUTE = {"weekday": {"depart": "07:00", "arrive": "18:00", "miles": 40},
           "weekend": {"depart": "10:00", "arrive": "15:00", "miles": 10}, "kwh_per_mile": 0.25}


def _grid(start="2026-01-05T00:00:00", days=7, minutes=60):  # 2026-01-05 is a Monday
    return TimeGrid(resolution_minutes=minutes, horizon_steps=days * 24 * 60 // minutes,
                    start=datetime.fromisoformat(start))


def _expand(raw=COMMUTE, grid=None, **kw):
    grid = grid or _grid()
    return expand_pattern(parse_pattern("ev", raw), grid, kw.get("capacity", 60), kw.get("p_max", 7.2)), grid


def test_weekdays_and_weekends():
    e, _ = _expand()
    monday, saturday = e.connected[:24], e.connected[5 * 24:6 * 24]
    assert list(np.flatnonzero(monday == 0)) == list(range(7, 18))  # away 07:00-18:00
    assert list(np.flatnonzero(saturday == 0)) == list(range(10, 15))
    assert e.departure_period == [7, 31, 55, 79, 103, 130, 154]
    assert e.driving_kwh[7:18].sum() == pytest.approx(40 * 0.25) and e.driving_kwh[130:135].sum() == pytest.approx(2.5)
    assert e.driving_kwh.sum() == pytest.approx(5 * 10 + 2 * 2.5)
    assert (e.driving_kwh[e.connected > 0] == 0).all()
    assert e.notes == []  # every time on the hour


def test_holidays_use_the_weekend_pattern_unless_set_to_weekday():
    grid = _grid("2026-01-01T00:00:00", days=1)  # New Year's Day, a Thursday
    weekend, _ = _expand(grid=grid)
    assert list(np.flatnonzero(weekend.connected == 0)) == list(range(10, 15))
    weekday, _ = _expand(COMMUTE | {"holidays": "weekday"}, grid=grid)
    assert list(np.flatnonzero(weekday.connected == 0)) == list(range(7, 18))


def test_home_days_and_night_shifts():
    e, _ = _expand(COMMUTE | {"weekend": "home"})
    assert e.connected[5 * 24:].all() and len(e.departure_period) == 5
    night = {"weekday": {"depart": "22:00", "arrive": "06:00", "miles": 30}, "weekend": "home", "kwh_per_mile": 0.3}
    n, _ = _expand(night)
    # Monday's shift runs 22:00-06:00 Tuesday; Friday's (a weekday) ends Saturday morning
    assert list(np.flatnonzero(n.connected[22:30] == 0)) == list(range(8))
    assert n.connected[4 * 24 + 22] == 0 and n.connected[5 * 24 + 5] == 0 and n.connected[5 * 24 + 6] == 1
    assert n.driving_kwh.sum() == pytest.approx(5 * 9)


def test_a_run_starting_mid_trip_uses_the_rest_of_it():
    e, _ = _expand(grid=_grid("2026-01-05T12:00:00", days=1))  # Monday noon, the car left at 07:00
    assert (e.connected[:6] == 0).all() and e.connected[6] == 1
    assert e.driving_kwh[:6].sum() == pytest.approx(10 * 6 / 11)  # 6 of the trip's 11 hours
    assert e.departure_period == [19]  # Tuesday 07:00


def test_off_step_times_round_toward_away_with_a_note():
    raw = COMMUTE | {"weekday": {"depart": "07:30", "arrive": "18:30", "miles": 40}}
    hourly, _ = _expand(raw)
    assert list(np.flatnonzero(hourly.connected[:24] == 0)) == list(range(7, 19))  # 07:00-19:00
    assert hourly.driving_kwh[:24].sum() == pytest.approx(10)
    (level, note), = hourly.notes
    assert level == "info" and "07:30 departure counts as 07:00 and 18:30 arrival counts as 19:00" in note
    assert "1 h less plug-in per day" in note
    quarter, _ = _expand(raw, grid=_grid(minutes=15))
    assert quarter.notes == [] and list(np.flatnonzero(quarter.connected[:96] == 0)) == list(range(30, 74))


def test_a_charger_too_slow_for_the_trip_warns():
    raw = COMMUTE | {"weekday": {"depart": "07:00", "arrive": "20:00", "miles": 80}}
    e, _ = _expand(raw, p_max=1.4)  # 11 h home x 1.4 kW x 0.95 = 14.6 kWh < 20 kWh
    (level, note), = e.notes
    assert level == "warning" and "the charger can put back only 14.6 kWh" in note


@pytest.mark.parametrize("change, message", [
    ({"weekday": {"depart": "7:30", "arrive": "18:00", "miles": 40}}, "isn't a time as HH:MM"),
    ({"weekday": {"depart": "07:00", "arrive": "07:00", "miles": 40}}, "a trip needs a length"),
    ({"weekday": {"depart": "07:00", "arrive": "18:00", "miles": -1}}, "greater than or equal to 0"),
    ({"kwh_per_mile": 0}, "greater than 0"),
    ({"weekday": {"depart": "07:00", "arrive": "18:00", "miles": 400}}, "more than the 60.0 kWh the battery can give"),
    ({"weekday": {"depart": "20:00", "arrive": "11:00", "miles": 10}}, "after the next trip leaves"),
])
def test_bad_patterns_are_errors(change, message):
    with pytest.raises(ValueError, match=message):
        _expand(COMMUTE | change)


def test_pattern_cant_be_combined_with_what_it_sets():
    params = {"capacity_kwh": 60, "p_max_charge_kw": 7.2, "p_max_discharge_kw": 0, "pattern": COMMUTE,
              "connected": [1] * 168}
    with pytest.raises(ValueError, match="remove connected or the pattern"):
        pattern_device_params("ev", params, _grid())


def test_departures_are_found_from_connected_and_enforced():
    grid = _grid(days=2)
    e, _ = _expand(grid=grid)
    ev = V2XVehicle("ev", grid, capacity_kwh=60, p_max_charge_kw=7.2, p_max_discharge_kw=7.2,
                    connected=e.connected, driving_kwh=e.driving_kwh, soc_init_frac=0.5,
                    soc_departure_min_frac=0.9, degradation_cost_per_kwh=0.01)
    assert ev.departure_periods == [7, 31]
    # export pays more than import, so without the departure rule the car would leave nearly empty
    premise = Premise("home", grid, [ev], [EnergyArbitrage(ConstantPriceSeries(0.10), ConstantPriceSeries(0.30), grid)],
                      external_load=np.zeros(grid.horizon_steps), poi_limit_kw=20)
    premise.solve()
    assert pyo.value(ev.soc[7]) >= 0.9 * 60 - 1e-6 and pyo.value(ev.soc[31]) >= 0.9 * 60 - 1e-6
    with pytest.raises(ValueError, match="needs departure_period to match"):
        V2XVehicle("ev", grid, 60, 7.2, 7.2, connected=e.connected, soc_departure_min_frac=[0.8, 0.8])


def test_example_runs_and_reports_the_pattern():
    scenario = REPO / "examples" / "scenarios" / "ev_commuter_annual.yaml"
    ev = next(d for d in load_scenario(scenario).devices if d.name == "ev")
    assert len(ev.departure_periods) == 365
    checked = runner.invoke(app, ["validate", str(scenario)])
    assert checked.exit_code == 0 and "INFO ev pattern: ev weekday: 07:30 departure counts as 07:00" in checked.output
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code == 0, result.output
    assert "EV ev (daily pattern): 365 departures" in result.output and "h on weekdays" in result.output


def test_pattern_on_another_device_is_an_error(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("""
name: s
time_grid: {resolution_minutes: 60, horizon_steps: 24, start: "2026-01-05T00:00:00"}
devices:
  - {type: battery, name: battery, params: {capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5,
     pattern: {weekday: home, weekend: home, kwh_per_mile: 0.3}}}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="`pattern` is for ev_v2x devices"):
        load_scenario(path)
