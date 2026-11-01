from datetime import datetime

import pytest

from der_opt.devices import BatteryStorage, SolarPV, V2XVehicle
from der_opt.premise import Premise
from der_opt.simulation import (
    CounterfactualSimulator,
    NoFlexibilityPolicy,
    RuleBasedEVChargingPolicy,
    SelfConsumptionBatteryPolicy,
)
from der_opt.time_grid import TimeGrid


def _scenario():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5, soc_init_frac=0.3,
    )
    solar = SolarPV("solar", tg, available_kw=[0.0, 3.0, 5.0])
    premise = Premise("house1", tg, devices=[battery, solar], external_load=[2.0, 2.0, 4.0], poi_limit_kw=30)
    return premise, tg


def test_no_flexibility_policy_leaves_solar_at_full_availability():
    premise, tg = _scenario()
    sim = CounterfactualSimulator(premise, policies={"battery": NoFlexibilityPolicy()})
    result = sim.run()
    assert result.device_dispatch["solar.p_gen"].to_list() == pytest.approx([0.0, 3.0, 5.0])
    assert result.device_dispatch["battery.net_power"].to_list() == pytest.approx([0.0, 0.0, 0.0])


def test_counterfactual_result_satisfies_energy_balance_every_period():
    premise, tg = _scenario()
    sim = CounterfactualSimulator(premise, policies={"battery": SelfConsumptionBatteryPolicy(5.0, 5.0)})
    result = sim.run()
    for i, t in enumerate(tg.periods):
        device_power = sum(result.device_dispatch[f"{d.name}.net_power"].iloc[i] for d in premise.devices)
        lhs = result.grid_import.iloc[i] - result.grid_export.iloc[i] + device_power
        assert lhs == pytest.approx(premise.external_load[t], abs=1e-6)


def test_self_consumption_battery_discharges_on_deficit_and_charges_on_surplus():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5, soc_init_frac=0.5,
    )
    solar = SolarPV("solar", tg, available_kw=[0.0, 5.0])
    premise = Premise("house1", tg, devices=[battery, solar], external_load=[2.0, 1.0], poi_limit_kw=30)
    sim = CounterfactualSimulator(premise, policies={"battery": SelfConsumptionBatteryPolicy(5.0, 5.0)})
    result = sim.run()

    # t=0: no solar, 2 kW load -> battery should discharge to cover the deficit
    assert result.device_dispatch["battery.p_dis"].iloc[0] == pytest.approx(2.0)
    # t=1: 5 kW solar, 1 kW load -> 4 kW surplus should charge the battery
    assert result.device_dispatch["battery.p_ch"].iloc[1] == pytest.approx(4.0)


def test_rule_based_ev_charges_at_max_rate_until_full():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        connected=[1, 1, 1], eta_charge=1.0, eta_discharge=1.0, soc_init_frac=0.0,
    )
    premise = Premise("house1", tg, devices=[ev], external_load=[0.0, 0.0, 0.0], poi_limit_kw=30)
    sim = CounterfactualSimulator(premise, policies={"ev": RuleBasedEVChargingPolicy(5.0, soc_max=10.0)})
    result = sim.run()

    assert result.device_dispatch["ev.p_ch"].to_list() == pytest.approx([5.0, 5.0, 0.0])
    assert result.device_dispatch["ev.p_dis"].to_list() == pytest.approx([0.0, 0.0, 0.0])


def test_simulator_result_has_same_schema_as_optimizer_result():
    premise, tg = _scenario()
    sim = CounterfactualSimulator(premise)
    result = sim.run()
    optimized, _model, _backend = premise.solve()

    assert set(result.device_dispatch.columns) == set(optimized.device_dispatch.columns)
    assert list(result.device_dispatch.index) == list(optimized.device_dispatch.index)
    assert result.solver_status == "simulated"
