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
