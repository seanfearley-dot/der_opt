import pyomo.environ as pyo
import pytest

from der_opt.devices import V2XVehicle
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def test_power_is_zero_outside_plugin_window():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=7, p_max_discharge_kw=7,
        connected=[1, 1, 0, 0], soc_init_frac=0.3,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ev.register(model)
    # cheapest possible to charge at every period if it were allowed
    model.obj = pyo.Objective(expr=sum(ev.p_ch[t] - ev.p_dis[t] for t in tg.periods), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)

    for t in (2, 3):
        assert pyo.value(ev.p_ch[t]) == pytest.approx(0.0)
        assert pyo.value(ev.p_dis[t]) == pytest.approx(0.0)


def test_departure_soc_requirement_is_enforced():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=10, p_max_discharge_kw=10,
        connected=[1, 1, 1], soc_init_frac=0.1,
        departure_period=2, soc_departure_min_frac=0.5,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ev.register(model)
    # discharging is "free" revenue in this toy objective, so without the departure
    # constraint the optimizer would drain the battery instead of meeting the target.
    model.obj = pyo.Objective(expr=sum(-ev.p_dis[t] for t in tg.periods), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)

    assert pyo.value(ev.soc[2]) >= 0.5 * 40 - 1e-6


def test_soc_final_min_is_enforced():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=10, p_max_discharge_kw=10,
        connected=[1, 1, 1], soc_init_frac=0.5, soc_final_min_frac=0.4,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ev.register(model)
    model.obj = pyo.Objective(expr=sum(-ev.p_dis[t] for t in tg.periods), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)

    assert pyo.value(ev.soc[3]) >= 0.4 * 40 - 1e-6


def test_without_soc_final_min_a_departure_met_earlier_does_not_protect_a_later_discharge():
    # The real gap this guards: departure_period only checks SOC at one period. A
    # discharge opportunity *after* that period (e.g. an evening price spike on a
    # single-day horizon, after that morning's departure requirement is already
    # satisfied) has nothing requiring it be recharged -- without soc_final_min_frac,
    # the vehicle drains toward soc_min by the end of the horizon for pure revenue.
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=10, p_max_discharge_kw=10,
        connected=[1, 1, 1, 1], soc_init_frac=0.5,
        departure_period=0, soc_departure_min_frac=0.1,  # trivially already satisfied
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ev.register(model)
    model.obj = pyo.Objective(expr=sum(-ev.p_dis[t] for t in tg.periods), sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)

    assert pyo.value(ev.soc[4]) == pytest.approx(0.0, abs=1e-6)


def test_step_respects_connection_state():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=7, p_max_discharge_kw=7,
        connected=[0, 1], soc_init_frac=0.3,
    )
    result = ev.step({"t": 0, "soc": 12.0}, {"p_ch": 7.0})
    assert result["p_ch"] == pytest.approx(0.0)
    assert result["soc"] == pytest.approx(12.0)
