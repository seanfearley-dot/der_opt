"""NBTBill: the optimizer's objective must equal the bill engine's net cost for the
dispatch it picks. Hand-checked cases use the simple tariff from test_billing.py:
generation $0.10, delivery $0.20, non-bypassable $0.02 per kWh, $1/day; export credit
generation $0.10 and delivery $0.01 per kWh on weekdays."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from der_opt.billing import NBTProgram, bill_from_dispatch, load_retail_tariff
from der_opt.billing.objective import NBTBill, NEM2Bill
from der_opt.devices import BatteryStorage, SolarPV
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import ConstantPriceSeries
from der_opt.time_grid import TimeGrid

REPO = Path(__file__).parent.parent.parent
E_ELEC = REPO / "examples" / "data" / "tariffs" / "pge_e_elec_2026-06.yaml"
EEC_2026 = REPO / "examples" / "data" / "export_rates/pge_nbt_eec_2026.csv"


def _fixed_dispatch_cost(tariff, program, load, **bill_kwargs):
    """No devices: the dispatch is the load itself, so the LP's only freedom is how
    credits are applied -- isolates the bill formulation."""
    tg = TimeGrid(resolution_minutes=60, horizon_steps=len(load), start=datetime(2026, 6, 1))  # a Monday
    stream = NBTBill(tariff, program, tg, **bill_kwargs)
    premise = Premise("house", tg, devices=[], value_streams=[stream], external_load=load, poi_limit_kw=10)
    dispatch, _, _ = premise.solve()
    engine = stream.evaluate_numeric(dispatch.grid_import.to_numpy(), dispatch.grid_export.to_numpy(), tg)
    return dispatch.objective_value, engine


TWO_DAYS = [1.0] * 24 + [-1.0] * 24  # import 24 kWh Monday, export 24 kWh Tuesday
CYCLES = {"cycle_start_dates": [datetime(2026, 6, 1).date(), datetime(2026, 6, 2).date()]}


def test_without_true_up_credit_cant_reach_back_to_earlier_charges(simple_tariff, simple_export_rates):
    program = NBTProgram(simple_export_rates)
    lp, engine = _fixed_dispatch_cost(simple_tariff, program, TWO_DAYS, relevant_period_cycles=12, **CYCLES)
    # Monday: 24 kWh x 0.32 + $1; Tuesday: $1, its credit has nothing to offset
    assert engine == pytest.approx(24 * 0.32 + 2)
    assert lp == pytest.approx(engine, abs=1e-6)


def test_with_true_up_later_credit_offsets_earlier_charges(simple_tariff, simple_export_rates):
    program = NBTProgram(simple_export_rates)
    lp, engine = _fixed_dispatch_cost(simple_tariff, program, TWO_DAYS, relevant_period_cycles=2, **CYCLES)
    # generation: 2.40 charged, 2.40 credited -> 0; delivery: 4.80 - 0.24; NBC 0.48; $2
    assert engine == pytest.approx(4.56 + 0.48 + 2)
    assert lp == pytest.approx(engine, abs=1e-6)


def test_net_surplus_compensation_is_in_the_objective(simple_tariff, simple_export_rates):
    program = NBTProgram(simple_export_rates, nsc_rate_per_kwh=0.04, nsc_debit_rate_per_kwh=0.07)
    load = [1.0] * 24 + [-2.0] * 24  # 24 kWh surplus
    lp, engine = _fixed_dispatch_cost(simple_tariff, program, load, relevant_period_cycles=2, **CYCLES)
    # delivery 4.80 - 0.48 = 4.32; NBC 0.48; $2; NSC credit 24 x 0.04
    assert engine == pytest.approx(4.32 + 0.48 + 2 - 0.96)
    assert lp == pytest.approx(engine, abs=1e-6)


def test_acc_plus_and_carry_in_are_in_the_objective(simple_tariff, simple_export_rates):
    program = NBTProgram(simple_export_rates, acc_plus_per_kwh=0.05, carry_in_delivery=1.0)
    lp, engine = _fixed_dispatch_cost(simple_tariff, program, TWO_DAYS, relevant_period_cycles=12, **CYCLES)
    # carry-in delivery covers $1 of Monday; Tuesday's $1.20 of ACC Plus can only cover
    # Tuesday's $1 bill (it rolls forward, never back)
    assert engine == pytest.approx(24 * 0.32 + 2 - 1.0 - 1.0)
    assert lp == pytest.approx(engine, abs=1e-6)


def _battery_solar_house(stream_factory):
    """Three July weekdays on real E-ELEC and 2026 NBT rates; solar well above load, so
    the household is a net exporter."""
    tg = TimeGrid(resolution_minutes=60, horizon_steps=72, start=datetime(2026, 7, 13))
    hours = np.arange(72) % 24
    solar_kw = np.clip(8 * np.sin((hours - 6) / 12 * np.pi), 0, None)
    load_kw = 1.0 + 1.5 * ((hours >= 17) & (hours < 22))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=13.5, p_max_charge_kw=5, p_max_discharge_kw=5,
        eta_charge=0.96, eta_discharge=0.96, degradation_cost_per_kwh=0.015,
    )
    solar = SolarPV("solar", tg, available_kw=solar_kw)
    premise = Premise(
        "house", tg, devices=[battery, solar], value_streams=[stream_factory(tg)],
        external_load=load_kw, poi_limit_kw=15, exclusive_import_export=True,
    )
    return premise, tg


def test_objective_equals_bill_on_real_rates_with_a_battery():
    tariff = load_retail_tariff(E_ELEC)
    program = NBTProgram(EEC_2026, acc_plus_per_kwh=0.0088)
    premise, tg = _battery_solar_house(lambda tg: NBTBill(tariff, program, tg))
    dispatch, _, _ = premise.solve()
    degradation = dispatch.device_dispatch["battery.cost"].sum()
    bill = bill_from_dispatch(dispatch, tg, tariff, program)
    assert dispatch.objective_value - degradation == pytest.approx(bill.summary["net_cost"], abs=1e-4)


def test_optimizing_the_bill_never_bills_more_than_optimizing_export_value():
    from der_opt.billing import RetailTariffPriceSeries
    from der_opt.integrations.pge_nbt_export import NBTExportPriceSeries

    tariff = load_retail_tariff(E_ELEC)
    program = NBTProgram(EEC_2026, acc_plus_per_kwh=0.0088)

    def bill_plus_degradation(stream_factory):
        premise, tg = _battery_solar_house(stream_factory)
        dispatch, _, _ = premise.solve()
        bill = bill_from_dispatch(dispatch, tg, tariff, program).summary["net_cost"]
        return bill + dispatch.device_dispatch["battery.cost"].sum()

    linear = bill_plus_degradation(lambda tg: EnergyArbitrage(
        RetailTariffPriceSeries(tariff), NBTExportPriceSeries(EEC_2026, adder_per_kwh=0.0088), tg
    ))
    bill_aware = bill_plus_degradation(lambda tg: NBTBill(tariff, program, tg))
    assert bill_aware <= linear + 1e-6


def test_energy_arbitrage_charges_energy_not_power_on_sub_hourly_grids():
    tg = TimeGrid(resolution_minutes=15, horizon_steps=4, start=datetime(2026, 6, 1))
    stream = EnergyArbitrage(ConstantPriceSeries(0.10), ConstantPriceSeries(0.0), tg)
    premise = Premise("house", tg, devices=[], value_streams=[stream], external_load=[2.0] * 4, poi_limit_kw=10)
    dispatch, _, _ = premise.solve()
    assert dispatch.objective_value == pytest.approx(2.0 * 1.0 * 0.10)  # 2 kW for an hour


# --- NEM2Bill ----------------------------------------------------------------------

E_TOU_C = REPO / "examples" / "data" / "tariffs" / "pge_e_tou_c_2026-06.yaml"


def _nem2_house(stream_factory, start=datetime(2026, 7, 13), days=3, solar_peak_kw=8.0):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24 * days, start=start)
    hours = np.arange(24 * days) % 24
    solar_kw = np.clip(solar_peak_kw * np.sin((hours - 6) / 12 * np.pi), 0, None)
    load_kw = 1.0 + 1.5 * ((hours >= 17) & (hours < 22))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=13.5, p_max_charge_kw=5, p_max_discharge_kw=5,
        eta_charge=0.96, eta_discharge=0.96, degradation_cost_per_kwh=0.015,
    )
    solar = SolarPV("solar", tg, available_kw=solar_kw)
    premise = Premise(
        "house", tg, devices=[battery, solar], value_streams=[stream_factory(tg)],
        external_load=load_kw, poi_limit_kw=15,
    )
    return premise, tg


@pytest.mark.parametrize("solar_peak_kw", [8.0, 2.0])  # net generator, net consumer
def test_nem2_objective_equals_bill_on_e_tou_c(solar_peak_kw):
    from der_opt.billing import NEM2Program

    tariff = load_retail_tariff(E_TOU_C)
    program = NEM2Program()
    premise, tg = _nem2_house(
        lambda tg: NEM2Bill(tariff, program, tg, baseline_territory="R"), solar_peak_kw=solar_peak_kw
    )
    dispatch, _, _ = premise.solve()
    degradation = dispatch.device_dispatch["battery.cost"].sum()
    bill = bill_from_dispatch(dispatch, tg, tariff, program, baseline_territory="R")
    assert dispatch.objective_value - degradation == pytest.approx(bill.summary["net_cost"], abs=1e-4)


def test_nem2_objective_equals_bill_with_baseline_on_both_sides_of_zero(tou_with_baseline):
    """Two cycles under a true-up: one a net consumer, one a net generator past its
    baseline allowance -- exercises every branch of the baseline formulation."""
    from datetime import date as _date

    from der_opt.billing import NEM2Program

    program = NEM2Program(nsc_rate_per_kwh=0.03)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=48, start=datetime(2026, 6, 1))
    load = [1.0] * 24 + [-2.0] * 24
    kwargs = {"baseline_territory": "X", "cycle_start_dates": [_date(2026, 6, 1), _date(2026, 6, 2)]}
    for cycles in (2, 12):
        stream = NEM2Bill(tou_with_baseline, program, tg, relevant_period_cycles=cycles, **kwargs)
        premise = Premise("house", tg, devices=[], value_streams=[stream], external_load=load, poi_limit_kw=10)
        dispatch, _, _ = premise.solve()
        engine = stream.evaluate_numeric(dispatch.grid_import.to_numpy(), dispatch.grid_export.to_numpy(), tg)
        assert dispatch.objective_value == pytest.approx(engine, abs=1e-6), cycles


def test_nem2_needs_a_baseline_territory_for_a_baseline_tariff():
    from der_opt.billing import NEM2Program

    tg = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 7, 1))
    with pytest.raises(ValueError, match="baseline_territory"):
        NEM2Bill(load_retail_tariff(E_TOU_C), NEM2Program(), tg)
