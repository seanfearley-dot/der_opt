import pyomo.environ as pyo
import pytest

from der_opt.devices import CurtailableLoad
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def test_respects_max_blocks_and_curtails_when_free():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    load = CurtailableLoad("pool", tg, baseline_load_kw=3.0, block_size_kw=1.0, max_blocks=3)
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    load.register(model)
    # curtailing is free (no penalty modeled) and reduces net_power's magnitude's
    # contribution; minimize p_served directly to push toward full curtailment.
    model.obj = pyo.Objective(expr=sum(load.p_served[t] for t in tg.periods), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)

    for t in tg.periods:
        n_blocks = pyo.value(load.n_blocks[t])
        assert 0 <= n_blocks <= 3
        assert pyo.value(load.p_served[t]) == pytest.approx(3.0 - n_blocks)
    assert pyo.value(load.n_blocks[0]) == pytest.approx(3)


def test_cannot_curtail_past_baseline():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    load = CurtailableLoad("pool", tg, baseline_load_kw=2.0, block_size_kw=1.0, max_blocks=5)
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    load.register(model)
    model.obj = pyo.Objective(expr=load.p_served[0], sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)
    # p_served has no explicit lower bound of 0 enforced beyond its own NonNegativeReals
    # domain -- max_blocks (5) * block_size (1) = 5 > baseline (2), so the solver should
    # stop curtailing once p_served would go negative, since p_served's domain forbids it.
    assert pyo.value(load.p_served[0]) == pytest.approx(0.0)
    assert pyo.value(load.n_blocks[0]) == pytest.approx(2.0)


def test_step_clips_n_blocks_to_valid_range():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    load = CurtailableLoad("pool", tg, baseline_load_kw=3.0, block_size_kw=1.0, max_blocks=2)
    result = load.step({"t": 0}, {"n_blocks": 10})
    assert result["n_blocks"] == 2
    assert result["p_served"] == pytest.approx(1.0)
