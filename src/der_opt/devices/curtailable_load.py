from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid


class CurtailableLoad(Device):
    """Discrete block curtailment: p_served[t] = baseline_load[t] - block_size_kw *
    n_blocks[t], with n_blocks an integer in [0, max_blocks]. Because n_blocks is
    integer, duals touching this device's constraints are not clean shadow prices --
    see der_opt.pricing.duals' fix-and-resolve handling. A minimum-curtailment-duration
    constraint (consecutive-period linking via an auxiliary binary) is a documented
    future extension, not needed for the MVP."""

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        baseline_load_kw,
        block_size_kw: float,
        max_blocks: int,
    ):
        super().__init__(name, time_grid)
        arr = np.asarray(baseline_load_kw, dtype=float)
        if arr.ndim == 0:
            arr = np.full(time_grid.horizon_steps, float(arr))
        if arr.shape[0] != time_grid.horizon_steps:
            raise ValueError("baseline_load_kw length must match time_grid.horizon_steps")
        self.baseline_load_kw = arr
        self.block_size_kw = block_size_kw
        self.max_blocks = max_blocks

    def build_vars(self, model: pyo.ConcreteModel) -> None:
        periods = self.time_grid.periods
        self.n_blocks = pyo.Var(periods, domain=pyo.NonNegativeIntegers, bounds=(0, self.max_blocks))
        self.p_served = pyo.Var(periods, domain=pyo.NonNegativeReals)
        model.add_component(self._component_name("n_blocks"), self.n_blocks)
        model.add_component(self._component_name("p_served"), self.p_served)

    def build_constraints(self, model: pyo.ConcreteModel) -> None:
        def served_rule(_m, t):
            return self.p_served[t] == self.baseline_load_kw[t] - self.block_size_kw * self.n_blocks[t]

        model.add_component(
            self._component_name("served"),
            pyo.Constraint(self.time_grid.periods, rule=served_rule),
        )

    def net_power_expr(self, model: pyo.ConcreteModel, t: int):
        return -self.p_served[t]

    def step(self, state: dict, action: dict) -> dict:
        t = state["t"]
        n_blocks = min(max(int(round(action.get("n_blocks", 0))), 0), self.max_blocks)
        p_served = self.baseline_load_kw[t] - self.block_size_kw * n_blocks
        return {"n_blocks": n_blocks, "p_served": p_served, "net_power": -p_served}

    def dispatch_series(self, model: pyo.ConcreteModel) -> dict[str, np.ndarray]:
        periods = self.time_grid.periods
        return {
            "n_blocks": np.array([pyo.value(self.n_blocks[t]) for t in periods]),
            "p_served": np.array([pyo.value(self.p_served[t]) for t in periods]),
            "net_power": np.array([-pyo.value(self.p_served[t]) for t in periods]),
        }
