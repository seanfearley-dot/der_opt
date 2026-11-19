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
