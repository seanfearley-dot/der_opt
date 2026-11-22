"""Covers the grid_import/grid_export exclusivity gap found while testing
charge_sources: by default the POI balance only links import and export by their
difference, so nothing stops both being positive in the same period -- harmless when
export price never exceeds import price, but a free-money degeneracy otherwise."""

from datetime import datetime

import pyomo.environ as pyo
import pytest

from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import CSVPriceSeries
from der_opt.time_grid import TimeGrid


def _degenerate_scenario(exclusive_import_export):
    # export (0.50) > import (0.10): without exclusivity, importing and exporting the
    # same period nets zero energy but earns a spread for free.
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    stream = EnergyArbitrage(CSVPriceSeries([0.10, 0.10]), CSVPriceSeries([0.50, 0.50]), tg)
    return Premise(
        "house", tg, devices=[], value_streams=[stream], external_load=[0.0, 0.0],
        poi_limit_kw=20, exclusive_import_export=exclusive_import_export,
    )


def test_default_allows_simultaneous_import_export_when_export_exceeds_import():
    premise = _degenerate_scenario(exclusive_import_export=False)
    dispatch, model, backend = premise.solve()
    # the degeneracy this test documents: both positive at once, for free profit
    assert pyo.value(premise.grid_import[0]) > 1e-6
    assert pyo.value(premise.grid_export[0]) > 1e-6


def test_exclusive_import_export_forbids_simultaneous_flow():
    premise = _degenerate_scenario(exclusive_import_export=True)
    dispatch, model, backend = premise.solve()
    for t in premise.time_grid.periods:
        imp = pyo.value(premise.grid_import[t])
        exp = pyo.value(premise.grid_export[t])
        assert imp < 1e-6 or exp < 1e-6
    # with no real load/devices and no legitimate way to profit, nothing should flow
    assert dispatch.objective_value == pytest.approx(0.0, abs=1e-6)


def test_exclusive_import_export_requires_poi_limit_kw():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    with pytest.raises(ValueError):
        Premise("house", tg, devices=[], external_load=[0.0, 0.0], exclusive_import_export=True)


# --- separate import and export limits -----------------------------------------------


def _solar_house(**limits):
    from der_opt.devices import SolarPV
    from der_opt.premise import EnergyArbitrage, Premise
    from der_opt.pricing import ConstantPriceSeries
    from der_opt.time_grid import TimeGrid

    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    solar = SolarPV("solar", tg, available_kw=[0.0, 8.0, 8.0])
    stream = EnergyArbitrage(ConstantPriceSeries(0.30), ConstantPriceSeries(0.10), tg)
    return Premise("h", tg, devices=[solar], value_streams=[stream], external_load=[6.0, 1.0, 1.0], **limits)


def test_import_and_export_limits_apply_separately():
    import pytest

    premise = _solar_house(import_limit_kw=10, export_limit_kw=3)
    dispatch, _, _ = premise.solve()
    assert dispatch.grid_import.iloc[0] == pytest.approx(6.0)  # 6 kW load within a 10 kW import limit
    assert dispatch.grid_export.max() == pytest.approx(3.0)  # 7 kW of surplus, 3 kW allowed out
    assert dispatch.device_dispatch["solar.p_gen"].iloc[1] == pytest.approx(4.0)  # the rest curtailed


def test_poi_limit_is_shorthand_and_specific_limits_override_it():
    premise = _solar_house(poi_limit_kw=20, export_limit_kw=0)
    assert (premise.import_limit_kw, premise.export_limit_kw) == (20, 0)
    dispatch, _, _ = premise.solve()
    assert dispatch.grid_export.max() == 0  # non-export system


def test_counterfactual_curtails_solar_at_the_export_limit():
    import pytest

    from der_opt.diagnostics import check_dispatch
    from der_opt.simulation import CounterfactualSimulator

    premise = _solar_house(import_limit_kw=10, export_limit_kw=3)
    simulated = CounterfactualSimulator(premise).run()
    assert simulated.grid_export.max() == pytest.approx(3.0)
    assert simulated.device_dispatch["solar.p_gen"].tolist() == pytest.approx([0.0, 4.0, 4.0])
    assert check_dispatch(premise, simulated).errors == []  # balances, within limits


def test_exclusive_import_export_needs_both_limits():
    import pytest

    with pytest.raises(ValueError, match="both meter limits"):
        _solar_house(import_limit_kw=10, exclusive_import_export=True)
    _solar_house(import_limit_kw=10, export_limit_kw=0, exclusive_import_export=True).solve()


def test_meter_limit_check_names_the_direction():
    import dataclasses

    from der_opt.diagnostics import check_dispatch

    premise = _solar_house(import_limit_kw=10, export_limit_kw=3)
    dispatch, _, _ = premise.solve()
    over = dataclasses.replace(dispatch, grid_export=dispatch.grid_export + 1, grid_import=dispatch.grid_import + 1)
    messages = [f.message for f in check_dispatch(premise, over).findings if f.check == "meter limit"]
    assert any(m.startswith("export reaches 4.000 kW, over its 3 kW limit") for m in messages)


# --- the single meter's on/off variable only where it matters --------------------------


def _priced_house(export_prices, stream_class=EnergyArbitrage):
    from der_opt.devices import BatteryStorage

    n = len(export_prices)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=n, start=datetime(2026, 8, 1))
    battery = BatteryStorage("battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5)
    stream = stream_class(CSVPriceSeries([0.30] * n), CSVPriceSeries(export_prices), tg)
    return Premise("house", tg, devices=[battery], value_streams=[stream], external_load=[1.0] * n,
                   poi_limit_kw=20, exclusive_import_export=True)


def _both_ways(dispatch):
    return ((dispatch.grid_import > 1e-6) & (dispatch.grid_export > 1e-6)).sum()


def test_the_on_off_variable_goes_only_where_export_pays_more():
    premise = _priced_house([0.10, 0.50, 0.10, 0.30])  # 0.30 is a tie: doing both gains nothing
    dispatch, _, _ = premise.solve()
    assert premise.meter_periods == {1} and _both_ways(dispatch) == 0


def test_a_retail_rate_meter_stays_a_linear_program():
    from der_opt.pricing.duals import extract_duals

    premise = _priced_house([0.10] * 4)
    dispatch, model, backend = premise.solve()
    assert premise.meter_periods == set() and _both_ways(dispatch) == 0
    assert extract_duals(model, backend, [f"{premise.name}_poi_balance"])[f"{premise.name}_poi_balance"].is_exact


def test_the_solve_adds_the_variable_where_a_stream_missed_a_gain():
    import numpy as np

    class Understated(EnergyArbitrage):
        def pass_through_gain(self, premise):
            return np.zeros(premise.time_grid.horizon_steps)  # wrong: export pays more at t=1

    exact, _, _ = _priced_house([0.10, 0.50, 0.10, 0.10]).solve()
    premise = _priced_house([0.10, 0.50, 0.10, 0.10], Understated)
    dispatch, _, _ = premise.solve()
    assert premise.meter_periods == {1} and _both_ways(dispatch) == 0
    assert dispatch.objective_value == pytest.approx(exact.objective_value)


def test_an_unknown_stream_gets_the_variable_everywhere():
    class Unknown(EnergyArbitrage):
        def pass_through_gain(self, premise):
            return None

    premise = _priced_house([0.10] * 4, Unknown)
    premise.solve()
    assert premise.meter_periods == {0, 1, 2, 3}


def test_nbt_meter_matches_on_off_in_every_hour():
    from der_opt.scenarios import load_scenario

    path = "examples/scenarios/battery_solar_lbnl_nbt.yaml"
    auto = load_scenario(path)
    dispatch, _, _ = auto.solve()
    everywhere = load_scenario(path)
    everywhere.periods_needing_meter = lambda: list(everywhere.time_grid.periods)
    reference, _, _ = everywhere.solve()
    assert 0 < len(auto.meter_periods) < auto.time_grid.horizon_steps // 10
    assert _both_ways(dispatch) == 0
    assert dispatch.objective_value == pytest.approx(reference.objective_value, rel=1e-4)
