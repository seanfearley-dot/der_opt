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
    NetworkDispatchModel (Phase 3) share this exact code unmodified.

    `exclusive_import_export`: by default, grid_import and grid_export are only linked
    by their *difference* in the POI balance -- nothing stops both being positive at
    the same period. That's harmless whenever the export price never exceeds the
    import price (true of essentially every flat/TOU retail tariff, since a
    cost-minimizing LP would never choose to pay import price to resell at a lower
    export price), but if export can exceed import at some period (e.g. a wholesale-LMP
    export price spike), the optimizer can "arbitrage" the POI itself for free money
    with no real energy flow, bypassing any devices entirely. Set this True to forbid
    that (adds one binary variable per period; makes the model a MILP, so unrelated
    duals go through the fix-and-resolve path -- off by default so a premise with no
    other integer variables keeps exact LP duals).

    Meter limits: `import_limit_kw` caps what the premise can draw from the grid (its
    service or panel rating) and `export_limit_kw` what it can send back (its
    interconnection agreement; 0 for a non-export system). `poi_limit_kw` is shorthand
    that sets both; a direction-specific limit overrides it. None = no limit."""

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        devices: list[Device],
        value_streams: list[ValueStream] | None = None,
        external_load=None,
        poi_limit_kw: float | None = None,
        exclusive_import_export: bool = False,
        import_limit_kw: float | None = None,
        export_limit_kw: float | None = None,
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
        self.import_limit_kw = import_limit_kw if import_limit_kw is not None else poi_limit_kw
        self.export_limit_kw = export_limit_kw if export_limit_kw is not None else poi_limit_kw
        for label, limit in (("import", self.import_limit_kw), ("export", self.export_limit_kw)):
            if limit is not None and limit < 0:
                raise ValueError(f"{label} limit must be >= 0, got {limit}")
        self.exclusive_import_export = exclusive_import_export
        if exclusive_import_export and (self.import_limit_kw is None or self.export_limit_kw is None):
            raise ValueError(
                "exclusive_import_export=True needs both meter limits (poi_limit_kw, or "
                "import_limit_kw and export_limit_kw): they're the big-M bounds on the "
                "import/export exclusivity constraint"
            )

        # populated by register()
        self.grid_import = None
        self.grid_export = None

    def register(self, model: pyo.ConcreteModel, prefix: str = "") -> None:
        """`prefix` namespaces this premise's device components (e.g. "house_01_"), so
        premises with identically named devices can share one model. The premise's own
        components already carry its name."""
        if not hasattr(model, "T"):
            model.T = pyo.Set(initialize=self.time_grid.periods)
        elif list(model.T) != list(self.time_grid.periods):
            raise ValueError(f"{self.name}'s time grid doesn't match the model's other premises")
        for device in self.devices:
            device.namespace = prefix

        # Two passes (all build_vars, then all build_constraints) rather than one
        # register() per device in sequence: lets a device's constraints reference
        # another device's variables (e.g. BatteryStorage's charge_sources constraint
        # reading a SolarPV's p_gen) regardless of which order they're listed in.
        for device in self.devices:
            device.build_vars(model)
        for device in self.devices:
            device.build_constraints(model)

        self._build_poi_constraints(model)

        for stream in self.value_streams:
            stream.apply(model, self)

    def _build_poi_constraints(self, model: pyo.ConcreteModel) -> None:
        periods = self.time_grid.periods

        self.grid_import = pyo.Var(periods, domain=pyo.NonNegativeReals, bounds=(0, self.import_limit_kw))
        self.grid_export = pyo.Var(periods, domain=pyo.NonNegativeReals, bounds=(0, self.export_limit_kw))
        model.add_component(f"{self.name}_grid_import", self.grid_import)
        model.add_component(f"{self.name}_grid_export", self.grid_export)

        def poi_rule(_m, t):
            device_power = sum(d.net_power_expr(_m, t) for d in self.devices)
            return self.grid_import[t] - self.grid_export[t] + device_power == self.external_load[t]

        self.poi_balance = pyo.Constraint(periods, rule=poi_rule)
        model.add_component(f"{self.name}_poi_balance", self.poi_balance)

        if self.exclusive_import_export:
            self.import_active = pyo.Var(periods, domain=pyo.Binary)
            model.add_component(f"{self.name}_import_active", self.import_active)

            def import_exclusive_rule(_m, t):
                return self.grid_import[t] <= self.import_limit_kw * self.import_active[t]

            def export_exclusive_rule(_m, t):
                return self.grid_export[t] <= self.export_limit_kw * (1 - self.import_active[t])

            model.add_component(
                f"{self.name}_import_exclusive", pyo.Constraint(periods, rule=import_exclusive_rule)
            )
            model.add_component(
                f"{self.name}_export_exclusive", pyo.Constraint(periods, rule=export_exclusive_rule)
            )

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
