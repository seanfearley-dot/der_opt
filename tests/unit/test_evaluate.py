from datetime import datetime

import pytest

from der_opt.devices import BatteryStorage, SolarPV
from der_opt.premise import (
    DemandCharge,
    EnergyArbitrage,
    EnergyTier,
    Premise,
    TariffEnergyCharge,
    TariffSchedule,
    TOUPeriodSchedule,
)
from der_opt.pricing import ConstantPriceSeries, CSVPriceSeries, value_breakdown
from der_opt.simulation import CounterfactualSimulator, NoFlexibilityPolicy
from der_opt.time_grid import TimeGrid


def test_value_breakdown_sums_to_objective_for_optimized_dispatch():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        degradation_cost_per_kwh=0.02,
    )
    solar = SolarPV("solar", tg, available_kw=[0, 2, 5, 1])
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.1, 0.5, 0.5]), ConstantPriceSeries(0.05), tg)
    premise = Premise(
        "house1", tg, devices=[battery, solar], value_streams=[stream],
        external_load=[1, 1, 3, 3], poi_limit_kw=20,
    )
    dispatch, model, backend = premise.solve()
    breakdown = value_breakdown(premise, dispatch)
    assert sum(breakdown.values()) == pytest.approx(dispatch.objective_value, abs=1e-6)


def test_tariff_energy_charge_evaluate_numeric_matches_symbolic_cost():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 6, 1))
    tariff = TariffSchedule(
        energy_periods={0: [EnergyTier(rate=0.10, max_kwh=2.0), EnergyTier(rate=0.50)]},
        energy_schedule=TOUPeriodSchedule.constant(0),
    )
    charge = TariffEnergyCharge(tariff, tg, direction="import")
    premise = Premise("house1", tg, devices=[], value_streams=[charge], external_load=[5.0])
    dispatch, model, backend = premise.solve()

    numeric_value = charge.evaluate_numeric(
        dispatch.grid_import.to_numpy(), dispatch.grid_export.to_numpy(), tg
    )
    assert numeric_value == pytest.approx(dispatch.objective_value, abs=1e-6)


def test_demand_charge_evaluate_numeric_matches_symbolic_cost():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    demand_charge = DemandCharge(rate_per_kw=10.0)
    premise = Premise(
        "house1", tg, devices=[], value_streams=[demand_charge], external_load=[2.0, 5.0, 3.0],
        poi_limit_kw=20,
    )
    dispatch, model, backend = premise.solve()
    numeric_value = demand_charge.evaluate_numeric(
        dispatch.grid_import.to_numpy(), dispatch.grid_export.to_numpy(), tg
    )
    assert numeric_value == pytest.approx(dispatch.objective_value, abs=1e-6)


def test_value_breakdown_includes_degradation_bucket_for_counterfactual_result():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        degradation_cost_per_kwh=0.1,
    )
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.5]), ConstantPriceSeries(0.0), tg)
    premise = Premise("house1", tg, devices=[battery], value_streams=[stream], external_load=[1.0, 1.0])

    sim = CounterfactualSimulator(premise, policies={"battery": NoFlexibilityPolicy()})
    cf_result = sim.run()
    breakdown = value_breakdown(premise, cf_result)
    assert breakdown["battery degradation"] == pytest.approx(0.0)
    assert "EnergyArbitrage" in breakdown
