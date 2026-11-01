from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid


class SolarPV(Device):
    """As-available, curtailable generation: p_gen[t] in [0, available_kw[t]]."""

    def __init__(self, name: str, time_grid: TimeGrid, available_kw):
        super().__init__(name, time_grid)
        arr = np.asarray(available_kw, dtype=float)
        if arr.shape[0] != time_grid.horizon_steps:
            raise ValueError(
                f"available_kw length {arr.shape[0]} does not match "
                f"time_grid.horizon_steps {time_grid.horizon_steps}"
            )
        self.available_kw = arr

    def build_vars(self, model: pyo.ConcreteModel) -> None:
        def bounds(_m, t):
            return (0, self.available_kw[t])

        self.p_gen = pyo.Var(self.time_grid.periods, bounds=bounds, domain=pyo.NonNegativeReals)
        model.add_component(self._component_name("p_gen"), self.p_gen)

    def build_constraints(self, model: pyo.ConcreteModel) -> None:
        pass  # variable bounds alone capture the physics

    def net_power_expr(self, model: pyo.ConcreteModel, t: int):
        return self.p_gen[t]

    def step(self, state: dict, action: dict) -> dict:
        t = state["t"]
        p_gen = min(max(action.get("p_gen", self.available_kw[t]), 0.0), self.available_kw[t])
        return {"p_gen": p_gen, "net_power": p_gen}

    def dispatch_series(self, model: pyo.ConcreteModel) -> dict[str, np.ndarray]:
        return {"p_gen": np.array([pyo.value(self.p_gen[t]) for t in self.time_grid.periods])}
