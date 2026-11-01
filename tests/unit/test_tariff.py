from datetime import datetime

import pyomo.environ as pyo
import pytest

from der_opt.premise import (
    CapacityReliabilityPayment,
    DemandCharge,
    EnergyTier,
    Premise,
    TariffEnergyCharge,
    TariffSchedule,
    TOUPeriodSchedule,
)
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def test_declining_tier_rates_are_rejected():
    with pytest.raises(ValueError):
        TariffSchedule(
            energy_periods={0: [EnergyTier(rate=0.30, max_kwh=1.0), EnergyTier(rate=0.10)]},
            energy_schedule=TOUPeriodSchedule.constant(0),
        )


def test_tiered_rate_fills_cheaper_tier_first():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2026, 6, 1))
    tariff = TariffSchedule(
        energy_periods={0: [EnergyTier(rate=0.10, max_kwh=2.0), EnergyTier(rate=0.50)]},
        energy_schedule=TOUPeriodSchedule.constant(0),
    )
    charge = TariffEnergyCharge(tariff, tg, direction="import")
    # no devices and a positive load forces grid_import[0] == load (5 kW for 1h = 5 kWh).
    premise = Premise("house1", tg, devices=[], value_streams=[charge], external_load=[5.0])

    model = premise.build_standalone_model()
    get_solver(SolverConfig(name="highs")).solve(model)

    tier0 = pyo.value(charge.tier_kwh[0, 0])
    tier1 = pyo.value(charge.tier_kwh[0, 1])
    assert tier0 == pytest.approx(2.0)  # tier 1's full 2 kWh capacity used first
    assert tier1 == pytest.approx(3.0)  # remaining 3 kWh spills into tier 2
    assert pyo.value(model.obj) == pytest.approx(0.10 * 2.0 + 0.50 * 3.0)


def test_from_urdb_json_parses_tou_tiers_and_flat_demand_charge():
    urdb_record = {
        "energyweekdayschedule": [[0] * 24 for _ in range(12)],
        "energyweekendschedule": [[0] * 24 for _ in range(12)],
        "energyratestructure": [
            [{"rate": 0.12, "max": 10, "sell": 0.04}, {"rate": 0.28}],
        ],
        "flatdemandstructure": [[{"rate": 12.5}]],
        "flatdemandmonths": [0] * 12,
    }
    tariff = TariffSchedule.from_urdb_json(urdb_record)
    assert tariff.is_tiered()
    assert tariff.energy_periods[0][0].rate == pytest.approx(0.12)
    assert tariff.energy_periods[0][0].max_kwh == pytest.approx(10)
    assert tariff.energy_periods[0][1].rate == pytest.approx(0.28)
    assert tariff.export_rate == pytest.approx(0.04)
    assert tariff.demand_charge_rate == pytest.approx(12.5)


def test_demand_charge_tracks_horizon_peak_import():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    demand_charge = DemandCharge(rate_per_kw=10.0)
    premise = Premise(
        "house1", tg, devices=[], value_streams=[demand_charge], external_load=[2.0, 5.0, 3.0],
        poi_limit_kw=20,
    )
    model = premise.build_standalone_model()
    get_solver(SolverConfig(name="highs")).solve(model)
    assert pyo.value(demand_charge.peak_demand) == pytest.approx(5.0)
    assert pyo.value(model.obj) == pytest.approx(10.0 * 5.0)


def test_capacity_reliability_payment_is_a_fixed_revenue_credit():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    payment = CapacityReliabilityPayment(event_periods=[1, 2], payment_per_kw_event=2.0, committed_kw=5.0)
    premise = Premise("house1", tg, devices=[], value_streams=[payment], external_load=[0.0, 0.0, 0.0])
    model = premise.build_standalone_model()
    get_solver(SolverConfig(name="highs")).solve(model)
    assert pyo.value(model.obj) == pytest.approx(-2.0 * 5.0 * 2)
