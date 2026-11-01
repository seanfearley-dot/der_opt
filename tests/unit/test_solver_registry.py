import pyomo.environ as pyo
import pytest

from der_opt.solvers import SolverConfig, get_solver


def _toy_model() -> pyo.ConcreteModel:
    model = pyo.ConcreteModel()
    model.x = pyo.Var(bounds=(0, 10))
    model.y = pyo.Var(bounds=(0, 10))
    model.c1 = pyo.Constraint(expr=model.x + model.y <= 10)
    model.obj = pyo.Objective(expr=2 * model.x + 3 * model.y, sense=pyo.maximize)
    return model


def test_appsi_highs_solves_toy_lp():
    model = _toy_model()
    backend = get_solver(SolverConfig(name="highs", use_persistent=True))
    backend.solve(model)

    assert pyo.value(model.x) == pytest.approx(0.0)
    assert pyo.value(model.y) == pytest.approx(10.0)
    assert pyo.value(model.obj) == pytest.approx(30.0)


def test_appsi_highs_get_duals_matches_expected_shadow_price():
    model = _toy_model()
    backend = get_solver(SolverConfig(name="highs", use_persistent=True))
    backend.solve(model)

    duals = backend.get_duals(model)
    assert duals[model.c1] == pytest.approx(2.0)


def test_legacy_highs_backend_also_solves_toy_lp():
    model = _toy_model()
    backend = get_solver(SolverConfig(name="highs", use_persistent=False))
    backend.solve(model)

    assert pyo.value(model.obj) == pytest.approx(30.0)


def test_unknown_solver_name_raises():
    with pytest.raises(ValueError):
        get_solver(SolverConfig(name="not-a-real-solver"))
