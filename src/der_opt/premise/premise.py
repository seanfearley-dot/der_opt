from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.premise.objective import ValueStream
from der_opt.results.extract import build_dispatch_result
from der_opt.solvers.interface import SolverConfig
from der_opt.solvers.registry import get_solver
from der_opt.time_grid import TimeGrid


class Premise:
    """Composes devices + an objective (value streams) + a point-of-interconnection
    balance into a Pyomo model. register(model, prefix) is the real composition seam:
    it adds everything onto a model it's handed rather than building its own, which is
    what lets build_standalone_model() (used here) and a future multi-premise
    NetworkDispatchModel (Phase 3) share this exact code unmodified."""

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        devices: list[Device],
        value_streams: list[ValueStream] | None = None,
        external_load=None,
        poi_limit_kw: float | None = None,
    ):
        self.name = name
        self.time_grid = time_grid
        self.devices = list(devices)
        self.value_streams = list(value_streams) if value_streams is not None else []
        if external_load is None:
            self.external_load = np.zeros(time_grid.horizon_steps)
        else:
            arr = np.asarray(external_load, dtype=float)
            if arr.shape[0] != time_grid.horizon_steps:
                raise ValueError(
                    f"external_load length {arr.shape[0]} does not match "
                    f"time_grid.horizon_steps {time_grid.horizon_steps}"
                )
            self.external_load = arr
        self.poi_limit_kw = poi_limit_kw

        # populated by register()
        self.grid_import = None
        self.grid_export = None

    def register(self, model: pyo.ConcreteModel, prefix: str = "") -> None:
        if not hasattr(model, "T"):
            model.T = pyo.Set(initialize=self.time_grid.periods)

        for device in self.devices:
            device.register(model)

        self._build_poi_constraints(model)

        for stream in self.value_streams:
            stream.apply(model, self)

    def _build_poi_constraints(self, model: pyo.ConcreteModel) -> None:
        bounds = (0, self.poi_limit_kw) if self.poi_limit_kw is not None else (0, None)
        periods = self.time_grid.periods

        self.grid_import = pyo.Var(periods, domain=pyo.NonNegativeReals, bounds=bounds)
        self.grid_export = pyo.Var(periods, domain=pyo.NonNegativeReals, bounds=bounds)
        model.add_component(f"{self.name}_grid_import", self.grid_import)
        model.add_component(f"{self.name}_grid_export", self.grid_export)

        def poi_rule(_m, t):
            device_power = sum(d.net_power_expr(_m, t) for d in self.devices)
            return self.grid_import[t] - self.grid_export[t] + device_power == self.external_load[t]

        self.poi_balance = pyo.Constraint(periods, rule=poi_rule)
        model.add_component(f"{self.name}_poi_balance", self.poi_balance)

    def total_cost_expr(self, model: pyo.ConcreteModel):
        device_cost = sum(
            device.cost_expr(model, t) for device in self.devices for t in self.time_grid.periods
        )
        stream_cost = sum(stream.total_cost_expr(model, self) for stream in self.value_streams)
        return device_cost + stream_cost

    def build_standalone_model(self) -> pyo.ConcreteModel:
        model = pyo.ConcreteModel()
        self.register(model)
        model.obj = pyo.Objective(expr=self.total_cost_expr(model), sense=pyo.minimize)
        return model

    def solve(self, solver_config: SolverConfig | None = None):
        """Returns (DispatchResult, model, backend) -- the model and backend are handed
        back too (not just the result) because dual extraction (der_opt.pricing.duals)
        needs both the solved model and the backend that solved it."""
        model = self.build_standalone_model()
        backend = get_solver(solver_config or SolverConfig())
        solver_result = backend.solve(model)
        dispatch = build_dispatch_result(self, model, solver_result)
        return dispatch, model, backend
