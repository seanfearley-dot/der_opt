import pyomo.environ as pyo
import pytest

from der_opt.devices import SolarPV
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def test_solar_curtails_to_minimize_cost_when_price_is_negative():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    solar = SolarPV("solar", tg, available_kw=[5.0, 5.0])

    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    solar.register(model)
    # negative price for generating (e.g. a curtailment-preferred period) at t=1
    model.obj = pyo.Objective(
        expr=1.0 * solar.p_gen[0] - 1.0 * solar.p_gen[1], sense=pyo.minimize
    )
    get_solver(SolverConfig(name="highs")).solve(model)

    assert pyo.value(solar.p_gen[0]) == pytest.approx(0.0)
    assert pyo.value(solar.p_gen[1]) == pytest.approx(5.0)


def test_solar_available_kw_length_validated():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    with pytest.raises(ValueError):
        SolarPV("solar", tg, available_kw=[1.0, 2.0])


def test_solar_step_clips_to_available_and_nonnegative():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    solar = SolarPV("solar", tg, available_kw=[2.0, 4.0])

    result = solar.step({"t": 0}, {"p_gen": 10.0})
    assert result["p_gen"] == pytest.approx(2.0)

    result = solar.step({"t": 1}, {"p_gen": -3.0})
    assert result["p_gen"] == pytest.approx(0.0)
