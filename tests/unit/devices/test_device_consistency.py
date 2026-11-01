"""Fixes the optimizer's Pyomo decision variables to a chosen action for one period and
checks the resulting next-state matches what step() computes directly -- the guarantee
that a device's symbolic (build_constraints) and numeric (step) physics never drift
apart, since CounterfactualSimulator relies on step() alone."""

import pyomo.environ as pyo
import pytest

from der_opt.devices import BatteryStorage, SmartThermostat, V2XVehicle
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def test_battery_step_matches_symbolic_soc_dynamics():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    battery = BatteryStorage(
        "battery", tg, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
        eta_charge=0.92, eta_discharge=0.93, soc_init_frac=0.4,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    battery.register(model)
    battery.p_ch[0].fix(1.5)
    battery.p_dis[0].fix(0.0)
    model.obj = pyo.Objective(expr=0.0)
    get_solver(SolverConfig(name="highs")).solve(model)

    symbolic_next_soc = pyo.value(battery.soc[1])
    numeric = battery.step(battery.initial_state(), {"p_ch": 1.5, "p_dis": 0.0})
    assert numeric["soc"] == pytest.approx(symbolic_next_soc, abs=1e-6)


def test_ev_step_matches_symbolic_soc_dynamics():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    ev = V2XVehicle(
        "ev", tg, capacity_kwh=40, p_max_charge_kw=7, p_max_discharge_kw=7,
        connected=[1], eta_charge=0.9, eta_discharge=0.9, soc_init_frac=0.2,
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ev.register(model)
    ev.p_ch[0].fix(4.0)
    ev.p_dis[0].fix(0.0)
    model.obj = pyo.Objective(expr=0.0)
    get_solver(SolverConfig(name="highs")).solve(model)

    symbolic_next_soc = pyo.value(ev.soc[1])
    numeric = ev.step({**ev.initial_state(), "t": 0}, {"p_ch": 4.0, "p_dis": 0.0})
    assert numeric["soc"] == pytest.approx(symbolic_next_soc, abs=1e-6)


def test_thermostat_step_matches_symbolic_temp_dynamics():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    ac = SmartThermostat(
        "ac", tg, thermal_resistance_r=2.0, thermal_capacitance_c=1.5,
        ambient_temp_c=[33.0], comfort_min_c=18.0, comfort_max_c=28.0,
        cop=3.0, p_max_kw=4.0, initial_temp_c=23.0, mode="cooling",
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ac.register(model)
    ac.p_hvac[0].fix(1.2)
    model.obj = pyo.Objective(expr=0.0)
    get_solver(SolverConfig(name="highs")).solve(model)

    symbolic_next_temp = pyo.value(ac.temp[1])
    numeric = ac.step({"t": 0, "temp": ac.initial_temp_c}, {"p_hvac": 1.2})
    assert numeric["temp"] == pytest.approx(symbolic_next_temp, abs=1e-6)
