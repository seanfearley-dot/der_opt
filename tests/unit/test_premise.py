from datetime import datetime

import pytest

from der_opt.devices import BatteryStorage, SolarPV
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import ConstantPriceSeries, CSVPriceSeries
from der_opt.time_grid import TimeGrid


def _scenario():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        degradation_cost_per_kwh=0.01,
    )
    solar = SolarPV("solar", tg, available_kw=[0, 2, 5, 1])
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.1, 0.5, 0.5]), ConstantPriceSeries(0.05), tg)
    premise = Premise(
        "house1", tg, devices=[battery, solar], value_streams=[stream],
        external_load=[1, 1, 3, 3], poi_limit_kw=20,
    )
    return premise, tg


def test_energy_balance_invariant_holds_every_period():
    premise, tg = _scenario()
    dispatch, model, backend = premise.solve()
    for i, t in enumerate(tg.periods):
        device_power = sum(d.net_power_expr(model, t) for d in premise.devices)
        from pyomo.environ import value

        lhs = value(premise.grid_import[t]) - value(premise.grid_export[t]) + value(device_power)
        assert lhs == pytest.approx(premise.external_load[t], abs=1e-6)


def test_solve_returns_optimal_status():
    premise, _ = _scenario()
    dispatch, model, backend = premise.solve()
    assert dispatch.solver_status == "TerminationCondition.optimal"


def test_external_load_length_is_validated():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4)
    with pytest.raises(ValueError):
        Premise("house1", tg, devices=[], external_load=[1, 2])


def test_device_dispatch_dataframe_has_expected_columns():
    premise, _ = _scenario()
    dispatch, model, backend = premise.solve()
    assert "battery.soc" in dispatch.device_dispatch.columns
    assert "solar.p_gen" in dispatch.device_dispatch.columns
    assert len(dispatch.device_dispatch) == premise.time_grid.horizon_steps
