"""Verifies charge_sources actually blocks grid-charge-then-export arbitrage. Uses a
load-avoidance price scenario (cheap import now, expensive import later, cheap export
always) rather than export > import: with export priced above import, nothing in
Premise's POI balance stops simultaneous import+export at the same period for free
money (see test_premise_poi.py::test_... for that separate finding) -- a degenerate
case these tests deliberately avoid so they isolate charge_sources' own behavior."""

from datetime import datetime

import pyomo.environ as pyo
import pytest

from der_opt.devices import BatteryStorage, SolarPV, V2XVehicle
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import CSVPriceSeries
from der_opt.time_grid import TimeGrid


def _arbitrage_scenario(charge_sources):
    # cheap import at t=0, expensive import at t=1, export always cheap (never
    # profitable to round-trip through export) -- so any charging at t=0 here can only
    # be legitimate "avoid expensive import later" arbitrage, not grid-charge-to-resell.
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    solar = SolarPV("solar", tg, available_kw=[0.0, 0.0])  # no solar available at all
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        soc_init_frac=0.0, degradation_cost_per_kwh=0.0,
        charge_sources=[solar] if charge_sources else None,
    )
    stream = EnergyArbitrage(CSVPriceSeries([0.10, 0.50]), CSVPriceSeries([0.02, 0.02]), tg)
    premise = Premise(
        "house", tg, devices=[battery, solar], value_streams=[stream],
        external_load=[0.0, 5.0], poi_limit_kw=20,
    )
    return premise, battery


def test_without_charge_sources_battery_arbitrages_from_grid():
    premise, battery = _arbitrage_scenario(charge_sources=False)
    dispatch, model, backend = premise.solve()
    assert pyo.value(battery.p_ch[0]) > 1e-6  # charges cheap at t=0 to avoid t=1's price


def test_charge_sources_blocks_grid_arbitrage_when_solar_is_unavailable():
    premise, battery = _arbitrage_scenario(charge_sources=True)
    dispatch, model, backend = premise.solve()
    for t in premise.time_grid.periods:
        assert pyo.value(battery.p_ch[t]) == pytest.approx(0.0, abs=1e-6)


def test_charge_sources_allows_charging_up_to_concurrent_solar():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    solar = SolarPV("solar", tg, available_kw=[4.0, 0.0])
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        soc_init_frac=0.0, charge_sources=[solar],
    )
    stream = EnergyArbitrage(CSVPriceSeries([0.10, 0.50]), CSVPriceSeries([0.02, 0.02]), tg)
    premise = Premise(
        "house", tg, devices=[battery, solar], value_streams=[stream],
        external_load=[0.0, 5.0], poi_limit_kw=20,
    )
    dispatch, model, backend = premise.solve()
    # charging is capped at concurrent solar (4 kW), not the battery's own 5 kW max
    assert pyo.value(battery.p_ch[0]) == pytest.approx(4.0, abs=1e-6)


def test_charge_sources_order_independent_in_devices_list():
    # battery listed BEFORE solar: only safe because Premise.register() now does two
    # passes (all build_vars, then all build_constraints) across its device list.
    premise, battery = _arbitrage_scenario(charge_sources=True)
    dispatch, model, backend = premise.solve()
    for t in premise.time_grid.periods:
        assert pyo.value(battery.p_ch[t]) == pytest.approx(0.0, abs=1e-6)


def test_v2x_charge_sources_blocks_grid_arbitrage():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    solar = SolarPV("solar", tg, available_kw=[0.0, 0.0])
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=7, p_max_discharge_kw=7,
        connected=[1, 1], soc_init_frac=0.0, charge_sources=[solar],
    )
    stream = EnergyArbitrage(CSVPriceSeries([0.10, 0.50]), CSVPriceSeries([0.02, 0.02]), tg)
    premise = Premise(
        "house", tg, devices=[ev, solar], value_streams=[stream],
        external_load=[0.0, 5.0], poi_limit_kw=20,
    )
    dispatch, model, backend = premise.solve()
    for t in premise.time_grid.periods:
        assert pyo.value(ev.p_ch[t]) == pytest.approx(0.0, abs=1e-6)
