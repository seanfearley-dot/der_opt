"""EV departure shortfall (docs/EV_SHORTFALL_SPEC.md v1.0): a hard target by default,
a priced shortfall down to a hard floor, departure shadow prices, and simulated runs
measured but not priced."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from der_opt.devices import V2XVehicle
from der_opt.pricing import value_breakdown
from der_opt.premise.objective import EnergyArbitrage
from der_opt.premise.premise import Premise
from der_opt.pricing.sources import ConstantPriceSeries, CSVPriceSeries
from der_opt.results.extract import ev_departures_table
from der_opt.simulation import CounterfactualSimulator
from der_opt.simulation.policy import RuleBasedEVChargingPolicy
from der_opt.time_grid import TimeGrid

GRID = TimeGrid(60, 12, datetime(2026, 7, 13, 20))
HOME = np.array([1] * 10 + [0, 0])  # home 20:00-06:00, leaves at 06:00
PRICES = np.array([0.40, 0.40] + [0.10] * 10)


def _home(limit=20.0, target=0.9, **params):
    ev = V2XVehicle("ev", GRID, capacity_kwh=75, p_max_charge_kw=7.2, p_max_discharge_kw=0, connected=HOME,
                    soc_init_frac=0.3, soc_departure_min_frac=target, **params)
    streams = [EnergyArbitrage(CSVPriceSeries(PRICES), ConstantPriceSeries(0.0), GRID)]
    return Premise("home", GRID, devices=[ev], external_load=np.full(12, 1.0), value_streams=streams,
                   poi_limit_kw=limit), ev


def _solve(**kwargs):
    premise, ev = _home(**kwargs)
    dispatch, _, _ = premise.solve()
    return premise, ev, dispatch, ev.departures(dispatch)[0]


def test_by_default_the_target_is_hard():
    with pytest.raises(RuntimeError):  # a 5 kW meter can't charge 45 kWh in ten hours
        _solve(limit=5.0)
    _, _, _, departure = _solve()
    assert departure["short_kwh"] == 0 and departure["stored_kwh"] == pytest.approx(67.5)


def test_a_priced_target_leaves_the_car_short_at_its_price():
    premise, ev, dispatch, departure = _solve(limit=5.0, departure_shortfall_per_kwh=0.5)
    assert departure["short_kwh"] == pytest.approx(7.0, abs=0.01)
    assert departure["shortfall_cost"] == pytest.approx(3.5, abs=0.01)
    assert departure["shadow_price_per_kwh"] == pytest.approx(0.5)  # while short, the price is the shortfall's
    breakdown = value_breakdown(premise, dispatch)  # its own line, not a stream or degradation
    assert breakdown["ev shortfall"] == pytest.approx(3.5, abs=0.01) and breakdown["ev degradation"] == 0


def test_the_floor_is_hard_priced_or_not():
    _, _, _, departure = _solve(limit=5.0, departure_shortfall_per_kwh=0.05, soc_departure_floor_frac=0.8)
    assert departure["stored_kwh"] >= 60.0 - 1e-6 and departure["floor_kwh"] == 60.0  # cheap shortfall, held at 80%
    with pytest.raises(RuntimeError):
        _solve(limit=5.0, departure_shortfall_per_kwh=0.5, soc_departure_floor_frac=0.85)


def test_the_departure_shadow_price_is_the_cost_of_one_more_kwh():
    premise, _, dispatch, departure = _solve(limit=5.0, target=0.75)  # hard, binding: uses the $0.40 hours
    _, _, nudged, _ = _solve(limit=5.0, target=0.75 + 1 / 75)
    assert departure["shadow_price_per_kwh"] == pytest.approx(nudged.objective_value - dispatch.objective_value,
                                                               rel=1e-3)
    assert departure["shadow_price_per_kwh"] == pytest.approx(0.40 / 0.95, rel=1e-3)


@pytest.mark.parametrize("params, message", [
    ({"departure_shortfall_per_kwh": -1.0}, "can't be negative"),
    ({"departure_shortfall_per_kwh": 0.5, "target": None}, "needs a departure target"),
    ({"soc_departure_floor_frac": 0.95}, "above the departure target"),
])
def test_settings_that_cant_work_are_rejected(params, message):
    with pytest.raises(ValueError, match=message):
        _home(**params)


def test_a_simulated_run_measures_the_shortfall_without_pricing_it():
    driving = np.zeros(12)
    driving[10:] = 40.0  # 80 kWh of driving: more than the battery can hold
    ev = V2XVehicle("ev", GRID, capacity_kwh=75, p_max_charge_kw=3.0, p_max_discharge_kw=0, connected=HOME,
                    soc_init_frac=0.3, soc_departure_min_frac=0.9, departure_shortfall_per_kwh=0.5,
                    driving_kwh=driving)
    premise = Premise("home", GRID, devices=[ev], external_load=np.full(12, 1.0),
                      value_streams=[EnergyArbitrage(CSVPriceSeries(PRICES), ConstantPriceSeries(0.0), GRID)])
    dispatch = CounterfactualSimulator(premise, {"ev": RuleBasedEVChargingPolicy(3.0, ev.soc_max)}).run()
    (departure,) = ev.departures(dispatch)
    assert departure["short_kwh"] > 15 and departure["shortfall_cost"] == 0  # 3 kW for ten hours falls short
    assert dispatch.device_dispatch["ev.driving_uncovered_kwh"].sum() > 0
    table = ev_departures_table([premise], {"counterfactual": {"home": dispatch}})
    assert list(table["run"]) == ["counterfactual"] and np.isnan(table["shadow_price_per_kwh"].iloc[0])


def test_a_feeder_counts_the_shortfall_apart_from_the_bill():
    from der_opt.scenarios.feeder import _house_costs, customer_cost

    premise, _, dispatch, _ = _solve(limit=5.0, departure_shortfall_per_kwh=0.5)
    costs = _house_costs([premise], {"home": dispatch})
    assert costs.loc["home", "ev_shortfall"] == pytest.approx(3.5, abs=0.01)
    assert customer_cost(costs)["home"] == pytest.approx(costs.loc["home", "bill"] + costs.loc["home", "degradation"])
    assert costs.loc["home"].sum() == pytest.approx(dispatch.objective_value, abs=0.01)  # all of it, once
