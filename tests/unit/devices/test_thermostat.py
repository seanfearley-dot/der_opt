import pyomo.environ as pyo
import pytest

from der_opt.devices import SmartThermostat
from der_opt.pricing import extract_duals
from der_opt.solvers import SolverConfig, get_solver
from der_opt.time_grid import TimeGrid


def _solved_cooling_model():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    ac = SmartThermostat(
        "ac", tg,
        thermal_resistance_r=1.0, thermal_capacitance_c=1.0,
        ambient_temp_c=[22.0, 35.0, 35.0], comfort_min_c=20.0, comfort_max_c=24.0,
        cop=3.0, p_max_kw=5.0, initial_temp_c=22.0, mode="cooling",
    )
    model = pyo.ConcreteModel()
    model.T = pyo.Set(initialize=tg.periods)
    ac.register(model)
    model.obj = pyo.Objective(expr=sum(0.1 * ac.p_hvac[t] for t in tg.periods), sense=pyo.minimize)
    backend = get_solver(SolverConfig(name="highs"))
    backend.solve(model)
    return ac, model, backend, tg


def test_temperature_never_leaves_comfort_band():
    ac, model, backend, tg = _solved_cooling_model()
    for t in tg.periods:
        temp = pyo.value(ac.temp[t])
        assert 20.0 - 1e-6 <= temp <= 24.0 + 1e-6


def test_comfort_band_dual_nonzero_only_when_binding():
    ac, model, backend, tg = _solved_cooling_model()
    duals = extract_duals(model, backend)
    comfort = duals["ac_comfort_band"].values
    for t in tg.periods:
        temp = pyo.value(ac.temp[t])
        binding = abs(temp - 24.0) < 1e-4 or abs(temp - 20.0) < 1e-4
        if binding:
            assert abs(comfort[t]) > 1e-6, f"expected a nonzero dual at t={t}"
        else:
            assert abs(comfort[t]) < 1e-6, f"expected a zero dual at t={t}"


def test_broadcast_rejects_mismatched_array_length():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    with pytest.raises(ValueError):
        SmartThermostat(
            "ac", tg, thermal_resistance_r=1.0, thermal_capacitance_c=1.0,
            ambient_temp_c=[22.0, 35.0], comfort_min_c=20.0, comfort_max_c=24.0,
            cop=3.0, p_max_kw=5.0, initial_temp_c=22.0,
        )


def test_a_cooling_thermostat_gets_non_electric_heat_on_cool_nights():
    # a hot afternoon the AC has to cool, then a cold night: without heat the house would
    # fall below 20 C; the non-electric heat holds it there and the AC stays off
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24)
    ambient = [32.0] * 8 + [5.0] * 16
    ac = SmartThermostat("ac", tg, thermal_resistance_r=2.5, thermal_capacitance_c=2.0, ambient_temp_c=ambient,
                         comfort_min_c=20.0, comfort_max_c=24.0, cop=3.0, p_max_kw=5.0, initial_temp_c=23.0)
    model = pyo.ConcreteModel()
    ac.register(model)
    model.obj = pyo.Objective(expr=sum(ac.p_hvac[t] for t in tg.periods) + ac.control_cost_expr(model),
                              sense=pyo.minimize)
    get_solver(SolverConfig(name="highs")).solve(model)
    temps = [pyo.value(ac.temp[t]) for t in tg.periods]
    other = [pyo.value(ac.other[t]) for t in tg.periods]
    assert all(20.0 - 1e-6 <= temp <= 24.0 + 1e-6 for temp in temps)
    assert sum(pyo.value(ac.p_hvac[t]) for t in range(8)) > 0  # cooled in the heat
    assert sum(other[8:]) > 0 and sum(other[:7]) == pytest.approx(0, abs=1e-6)  # heat only when it's cold
    assert all(pyo.value(ac.p_hvac[t]) == pytest.approx(0, abs=1e-6) for t in range(9, 24))
