from datetime import datetime

import pytest

from der_opt.devices import CurtailableLoad
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import ConstantPriceSeries, CSVPriceSeries, extract_duals
from der_opt.time_grid import TimeGrid


def test_energy_balance_dual_matches_marginal_import_price():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 6, 1))
    prices = [0.10, 0.30, 0.50]
    stream = EnergyArbitrage(CSVPriceSeries(prices), ConstantPriceSeries(0.0), tg)
    premise = Premise(
        "house1", tg, devices=[], value_streams=[stream], external_load=[1.0, 1.0, 1.0],
        poi_limit_kw=20,
    )
    dispatch, model, backend = premise.solve()
    duals = extract_duals(model, backend)
    poi_duals = duals["house1_poi_balance"].values
    for t, price in enumerate(prices):
        assert poi_duals[t] == pytest.approx(price, abs=1e-6)


def test_fix_and_resolve_flags_inexact_duals_with_integer_vars():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    load = CurtailableLoad("pool", tg, baseline_load_kw=2.0, block_size_kw=1.0, max_blocks=2)
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.5]), ConstantPriceSeries(0.0), tg)
    premise = Premise("house1", tg, devices=[load], value_streams=[stream], external_load=[0.0, 0.0])
    dispatch, model, backend = premise.solve()

    duals = extract_duals(model, backend)
    for dual in duals.values():
        assert dual.is_exact is False
        assert "fix-and-resolve" in dual.note.lower()


def test_exact_duals_when_no_integer_vars_present():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 6, 1))
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.5]), ConstantPriceSeries(0.0), tg)
    premise = Premise("house1", tg, devices=[], value_streams=[stream], external_load=[1.0, 1.0])
    dispatch, model, backend = premise.solve()

    duals = extract_duals(model, backend)
    for dual in duals.values():
        assert dual.is_exact is True
        assert dual.note == ""
