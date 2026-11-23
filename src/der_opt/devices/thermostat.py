from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid


class SmartThermostat(Device):
    """1R1C thermal model: temp[t+1] = temp[t] + (dt/C) * ((T_amb[t]-temp[t])/R +/- COP*p_hvac[t]),
    with a comfort-band inequality T_min[t] <= temp[t] <= T_max[t]. Because the comfort
    band is an inequality (not an equality), its dual is directly the $/degC marginal
    value of comfort at each period -- the intended demonstration case for the dual
    story. `mode="heating"` adds COP*p_hvac to the thermal balance; `mode="cooling"`
    subtracts it.

    A thermostat that only cools can't hold the lower comfort limit when the house is
    colder than that on its own (winter, with heating that isn't electric or isn't
    modeled), so there the lower limit is the free-floating temperature, the house with
    the HVAC off; a heating thermostat's upper limit likewise. A run that holds the band
    everywhere is unchanged: cooling only ever lowers the temperature below free-floating."""

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        thermal_resistance_r: float,
        thermal_capacitance_c: float,
        ambient_temp_c,
        comfort_min_c,
        comfort_max_c,
        cop: float,
        p_max_kw: float,
        initial_temp_c: float,
        mode: str = "cooling",
    ):
        super().__init__(name, time_grid)
        if mode not in ("heating", "cooling"):
            raise ValueError("mode must be 'heating' or 'cooling'")
        self.R = thermal_resistance_r
        self.C = thermal_capacitance_c
        self.T_amb = self._broadcast(ambient_temp_c, time_grid)
        self.T_min = self._broadcast(comfort_min_c, time_grid)
        self.T_max = self._broadcast(comfort_max_c, time_grid)
        self.cop = cop
        self.p_max_kw = p_max_kw
        self.initial_temp_c = initial_temp_c
        self.sign = 1.0 if mode == "heating" else -1.0
        free = self.free_floating()
        if mode == "cooling":
            self.T_min = np.minimum(self.T_min, free)
        else:
            self.T_max = np.maximum(self.T_max, free)

    def free_floating(self) -> np.ndarray:
        """Indoor temperature at the start of each step with the HVAC off."""
        dt = self.time_grid.dt_hours
        out = np.empty(len(self.T_amb))
        temp = self.initial_temp_c
        for t, ambient in enumerate(self.T_amb):
            out[t] = temp
            temp = temp + (dt / self.C) * (ambient - temp) / self.R
        return out

    @staticmethod
    def _broadcast(value, time_grid: TimeGrid) -> np.ndarray:
        arr = np.asarray(value, dtype=float)
        if arr.ndim == 0:
            return np.full(time_grid.horizon_steps, float(arr))
        if arr.shape[0] != time_grid.horizon_steps:
            raise ValueError("array input length must match time_grid.horizon_steps")
        return arr

    def build_vars(self, model: pyo.ConcreteModel) -> None:
        periods = self.time_grid.periods
        temp_periods = range(self.time_grid.horizon_steps + 1)
        self.p_hvac = pyo.Var(periods, bounds=(0, self.p_max_kw), domain=pyo.NonNegativeReals)
        self.temp = pyo.Var(temp_periods, domain=pyo.Reals)
        model.add_component(self._component_name("p_hvac"), self.p_hvac)
        model.add_component(self._component_name("temp"), self.temp)

    def build_constraints(self, model: pyo.ConcreteModel) -> None:
        dt = self.time_grid.dt_hours

        def temp_init_rule(_m):
            return self.temp[0] == self.initial_temp_c

        model.add_component(self._component_name("temp_init"), pyo.Constraint(rule=temp_init_rule))

        def temp_dynamics_rule(_m, t):
            return self.temp[t + 1] == self.temp[t] + (dt / self.C) * (
                (self.T_amb[t] - self.temp[t]) / self.R + self.sign * self.cop * self.p_hvac[t]
            )

        model.add_component(
            self._component_name("temp_dynamics"),
            pyo.Constraint(self.time_grid.periods, rule=temp_dynamics_rule),
        )

        def comfort_band_rule(_m, t):
            return (self.T_min[t], self.temp[t], self.T_max[t])

        model.add_component(
            self._component_name("comfort_band"),
            pyo.Constraint(self.time_grid.periods, rule=comfort_band_rule),
        )

    def net_power_expr(self, model: pyo.ConcreteModel, t: int):
        return -self.p_hvac[t]

    def initial_state(self) -> dict:
        return {"temp": self.initial_temp_c}

    def step(self, state: dict, action: dict) -> dict:
        t = state["t"]
        dt = self.time_grid.dt_hours
        temp = state["temp"]
        p_hvac = min(max(action.get("p_hvac", 0.0), 0.0), self.p_max_kw)
        next_temp = temp + (dt / self.C) * (
            (self.T_amb[t] - temp) / self.R + self.sign * self.cop * p_hvac
        )
        return {"temp": next_temp, "p_hvac": p_hvac, "net_power": -p_hvac}

    def dispatch_series(self, model: pyo.ConcreteModel) -> dict[str, np.ndarray]:
        periods = self.time_grid.periods
        return {
            "p_hvac": np.array([pyo.value(self.p_hvac[t]) for t in periods]),
            "temp": np.array([pyo.value(self.temp[t]) for t in periods]),
            "net_power": np.array([-pyo.value(self.p_hvac[t]) for t in periods]),
        }
