import pyomo.environ as pyo
import pytest

from der_opt.devices import BatteryStorage
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def _solve_toy_arbitrage(battery, prices):
    tg = battery.time_grid
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    battery.register(model)
    model.obj = pyo.Objective(
        expr=sum(
            prices[t] * (battery.p_ch[t] - battery.p_dis[t]) + battery.cost_expr(model, t)
            for t in tg.periods
        ),
        sense=pyo.minimize,
    )
    get_solver(SolverConfig(name="highs")).solve(model)
    return model


def test_battery_soc_recursion_matches_hand_calc():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        eta_charge=0.9, eta_discharge=0.9, soc_init_frac=0.5,
    )
    model = _solve_toy_arbitrage(battery, prices=[0.0, 0.0])
    # no price signal and no load: optimizer is indifferent, so force a known action
    # and check the recursion directly instead of relying on the optimum.
    battery.p_ch[0].fix(2.0)
    battery.p_dis[0].fix(0.0)
    get_solver(SolverConfig(name="highs")).solve(model)
    expected_soc_1 = 5.0 + 1.0 * (0.9 * 2.0 - 0.0)
    assert pyo.value(battery.soc[1]) == pytest.approx(expected_soc_1)


def test_battery_soc_stays_within_bounds():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4)
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        soc_min_frac=0.1, soc_max_frac=0.9, soc_init_frac=0.5,
    )
    model = _solve_toy_arbitrage(battery, prices=[0.5, 0.1, 0.1, 0.5])
    for t in range(5):
        soc = pyo.value(battery.soc[t])
        assert 0.1 * 10 - 1e-6 <= soc <= 0.9 * 10 + 1e-6


def test_battery_never_charges_and_discharges_simultaneously():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4)
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        eta_charge=0.9, eta_discharge=0.9, soc_init_frac=0.5, degradation_cost_per_kwh=0.0,
    )
    _solve_toy_arbitrage(battery, prices=[0.5, 0.1, 0.1, 0.5])
    for t in tg.periods:
        p_ch = pyo.value(battery.p_ch[t])
        p_dis = pyo.value(battery.p_dis[t])
        assert p_ch < 1e-6 or p_dis < 1e-6


def test_battery_degradation_cost_appears_in_realized_cost():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        soc_init_frac=0.5, degradation_cost_per_kwh=0.5,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    battery.register(model)
    battery.p_ch[0].fix(2.0)
    battery.p_dis[0].fix(0.0)
    model.obj = pyo.Objective(expr=battery.cost_expr(model, 0), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)
    assert pyo.value(model.obj) == pytest.approx(0.5 * 2.0 * 1.0)
