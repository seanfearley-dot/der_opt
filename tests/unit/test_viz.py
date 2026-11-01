from datetime import datetime

import matplotlib.figure
import pytest

from der_opt.devices import BatteryStorage, SmartThermostat, SolarPV
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import (
    ConstantPriceSeries,
    CSVPriceSeries,
    extract_duals,
    value_breakdown,
)
from der_opt.simulation import CounterfactualSimulator, SelfConsumptionBatteryPolicy
from der_opt.time_grid import TimeGrid
from der_opt.viz import (
    comparison_plot,
    dispatch_plot,
    duals_plot,
    interval_heatmap,
    value_stack,
)


@pytest.fixture
def solved_scenario():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=6, start=datetime(2026, 6, 1))
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        degradation_cost_per_kwh=0.02,
    )
    solar = SolarPV("solar", tg, available_kw=[0, 1, 3, 5, 3, 1])
    ac = SmartThermostat(
        "ac", tg, thermal_resistance_r=2.0, thermal_capacitance_c=2.0,
        ambient_temp_c=[30.0] * 6, comfort_min_c=20.0, comfort_max_c=24.0,
        cop=3.0, p_max_kw=3.0, initial_temp_c=22.0,
    )
    stream = EnergyArbitrage(CSVPriceSeries([0.1, 0.1, 0.3, 0.5, 0.3, 0.1]), ConstantPriceSeries(0.04), tg)
    premise = Premise(
        "house1", tg, devices=[battery, solar, ac], value_streams=[stream],
        external_load=[2, 2, 4, 4, 3, 2], poi_limit_kw=30,
    )
    dispatch, model, backend = premise.solve()
    return premise, tg, dispatch, model, backend


def test_interval_heatmap_returns_figure(solved_scenario):
    _premise, _tg, dispatch, _model, _backend = solved_scenario
    fig = interval_heatmap(dispatch.device_dispatch["solar.p_gen"], title="Solar")
    assert isinstance(fig, matplotlib.figure.Figure)


def test_interval_heatmap_requires_datetime_index():
    import pandas as pd

    series = pd.Series([1, 2, 3])
    with pytest.raises(ValueError):
        interval_heatmap(series)


def test_dispatch_plot_returns_figure(solved_scenario):
    premise, _tg, dispatch, _model, _backend = solved_scenario
    fig = dispatch_plot(dispatch, price=[0.1, 0.1, 0.3, 0.5, 0.3, 0.1], title="Dispatch")
    assert isinstance(fig, matplotlib.figure.Figure)


def test_value_stack_returns_figure(solved_scenario):
    premise, _tg, dispatch, _model, _backend = solved_scenario
    breakdown = value_breakdown(premise, dispatch)
    fig = value_stack(breakdown, title="Value stack")
    assert isinstance(fig, matplotlib.figure.Figure)


def test_duals_plot_returns_figure(solved_scenario):
    premise, _tg, dispatch, model, backend = solved_scenario
    duals = extract_duals(model, backend)
    fig = duals_plot(duals, names=["ac_comfort_band"], title="Duals")
    assert isinstance(fig, matplotlib.figure.Figure)


def test_comparison_plot_returns_figure_with_and_without_premise(solved_scenario):
    premise, _tg, dispatch, _model, _backend = solved_scenario
    sim = CounterfactualSimulator(premise, policies={"battery": SelfConsumptionBatteryPolicy(5.0, 5.0)})
    cf_result = sim.run()

    fig_with_breakdown = comparison_plot(dispatch, cf_result, premise=premise)
    assert isinstance(fig_with_breakdown, matplotlib.figure.Figure)
    assert len(fig_with_breakdown.axes) >= 2

    fig_without_breakdown = comparison_plot(dispatch, cf_result)
    assert isinstance(fig_without_breakdown, matplotlib.figure.Figure)
