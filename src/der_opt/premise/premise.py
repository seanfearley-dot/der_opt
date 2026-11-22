from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.premise.objective import ValueStream
from der_opt.results.extract import build_dispatch_result
from der_opt.solvers.interface import SolverConfig
from der_opt.solvers.registry import get_solver
from der_opt.time_grid import TimeGrid

PASS_THROUGH_TOLERANCE = 1e-9  # $ per kW-period: a gain above this gets the on/off variable
SIMULTANEOUS_FLOW_KW = 1e-6
MAX_METER_ROUNDS = 5


def solve_single_meter(backend, model: pyo.ConcreteModel, premises) -> object:
    """Solves a model with exclusive_import_export premises in it.

    Both steps rest on one fact: a model with fewer of the meter's constraints is a
    relaxation of the one with them, so if its solution never imports and exports at
    once, that solution is optimal for the full single meter too.

    1. Solve with the on/off variables relaxed to [0, 1]. That's often already a
       single-meter solution (it was on the annual NBT example), and then the MIP
       never runs: HiGHS can spend a minute in cut rounds on a MIP whose relaxation
       is the answer.
    2. Otherwise, solve the MIP.

    After either, a period left without the on/off variable where the solution still
    does both gets one, and the solve starts again (from MAX_METER_ROUNDS on, every
    period gets one)."""
    premises = [p for p in premises if p.exclusive_import_export]
    result = None
    for round_ in range(MAX_METER_ROUNDS + 1):
        result = _solve_relaxed(backend, model, premises)
        if result is not None and _is_optimal(result):
            if not any(p.simultaneous_flow(metered=True) or p.unmetered_simultaneous_flow() for p in premises):
                for premise in premises:
                    premise.set_meter_from_flows()
                return result
            if _add_where_both(model, premises, round_):
                continue
        if result is None or premises:
            result = backend.solve(model)
        if not _add_where_both(model, premises, round_):
            return result
    return result


def _solve_relaxed(backend, model, premises):
    variables = [v for p in premises for var in p.meter_variables for v in var.values()]
    if not variables:
        return None
    for v in variables:
        v.domain = pyo.UnitInterval
    try:
        return backend.solve(model)
    except (RuntimeError, ValueError):  # e.g. a backend that raises on no solution
        return None
    finally:
        for v in variables:
            v.domain = pyo.Binary


def _add_where_both(model, premises, round_) -> bool:
    added = False
    for premise in premises:
        periods = premise.unmetered_simultaneous_flow()
        if periods:
            premise.add_meter_binaries(model, premise.time_grid.periods if round_ >= MAX_METER_ROUNDS - 1 else periods)
            added = True
    return added


def _is_optimal(result) -> bool:
    from der_opt.results.extract import _solver_status

    return _solver_status(result).rsplit(".", 1)[-1] == "optimal"


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
    export price spike, or an NBT export credit above the import rate), the optimizer
    can "arbitrage" the POI itself for free money with no real energy flow, bypassing
    any devices entirely. Set this True to forbid that: a single meter, which either
    imports or exports in each period.

    It takes an on/off (binary) variable, which makes the model a MILP, but only in the
    periods where it can matter: where the value streams' pass_through_gain says doing
    both would pay. Elsewhere doing both costs money, so the optimum doesn't do it;
    solve_single_meter adds the variable in any other period where a solution still
    does both (a tie, or a gain the estimate missed) and solves again. A premise whose streams
    never pay for it stays an LP, with exact duals.

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
        self.meter_periods: set[int] = set()  # periods with the single-meter on/off variable

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

        self.meter_periods = set()
        self.meter_variables = []
        self._meter_round = 0
        if self.exclusive_import_export:
            self.add_meter_binaries(model, self.periods_needing_meter())

    def periods_needing_meter(self) -> list[int]:
        """The periods where importing and exporting at once could pay, by the value
        streams' own bounds (all of them if any stream can't say)."""
        gain = np.zeros(self.time_grid.horizon_steps)
        for stream in self.value_streams:
            bound = stream.pass_through_gain(self)
            if bound is None:
                return list(self.time_grid.periods)
            gain = gain + np.asarray(bound, dtype=float)
        return [int(t) for t in np.flatnonzero(gain > PASS_THROUGH_TOLERANCE)]

    def add_meter_binaries(self, model: pyo.ConcreteModel, periods) -> None:
        """Import or export, not both, in these periods: one binary each, with the meter
        limits as big-M bounds."""
        periods = sorted({int(t) for t in periods} - self.meter_periods)
        if not periods:
            return
        self._meter_round += 1
        suffix = "" if self._meter_round == 1 else f"_{self._meter_round}"
        active = pyo.Var(periods, domain=pyo.Binary)
        model.add_component(f"{self.name}_import_active{suffix}", active)
        model.add_component(f"{self.name}_import_exclusive{suffix}", pyo.Constraint(
            periods, rule=lambda _m, t: self.grid_import[t] <= self.import_limit_kw * active[t]))
        model.add_component(f"{self.name}_export_exclusive{suffix}", pyo.Constraint(
            periods, rule=lambda _m, t: self.grid_export[t] <= self.export_limit_kw * (1 - active[t])))
        self.meter_periods |= set(periods)
        self.meter_variables.append(active)

    def simultaneous_flow(self, metered: bool) -> list[int]:
        """Periods (with the on/off variable, or without) where the solved model
        imports and exports at once."""
        out = []
        for t in self.time_grid.periods:
            if (t in self.meter_periods) != metered:
                continue
            imp, exp = self.grid_import[t].value, self.grid_export[t].value
            if imp is not None and exp is not None and min(imp, exp) > SIMULTANEOUS_FLOW_KW:
                out.append(t)
        return out

    def unmetered_simultaneous_flow(self) -> list[int]:
        return self.simultaneous_flow(metered=False) if self.exclusive_import_export else []

    def set_meter_from_flows(self) -> None:
        """Each on/off variable to 1 where the meter imports, 0 where it exports (after
        a relaxed solve that already did one or the other)."""
        for active in self.meter_variables:
            for t in active:
                active[t].value = 1.0 if (self.grid_import[t].value or 0.0) >= (self.grid_export[t].value or 0.0) else 0.0

    def total_cost_expr(self, model: pyo.ConcreteModel):
        device_cost = sum(
            device.cost_expr(model, t) for device in self.devices for t in self.time_grid.periods
        )
        stream_cost = sum(stream.total_cost_expr(model, self) for stream in self.value_streams)
        return device_cost + stream_cost

    def control_cost_expr(self, model: pyo.ConcreteModel):
        """Costs that shape the dispatch but aren't real dollars (device smoothing):
        in the objective, never in a reported cost. Call once per model: it adds the
        variables it needs."""
        return sum(device.control_cost_expr(model) for device in self.devices)

    def build_standalone_model(self) -> pyo.ConcreteModel:
        model = pyo.ConcreteModel()
        self.register(model)
        self._control_cost = self.control_cost_expr(model)
        model.obj = pyo.Objective(expr=self.total_cost_expr(model) + self._control_cost, sense=pyo.minimize)
        return model

    def solve(self, solver_config: SolverConfig | None = None):
        """Returns (DispatchResult, model, backend) -- the model and backend are handed
        back too (not just the result) because dual extraction (der_opt.pricing.duals)
        needs both the solved model and the backend that solved it."""
        model = self.build_standalone_model()
        backend = get_solver(solver_config or SolverConfig())
        solver_result = solve_single_meter(backend, model, [self])
        dispatch = build_dispatch_result(self, model, solver_result,
                                         objective_value=pyo.value(model.obj) - pyo.value(self._control_cost))
        return dispatch, model, backend
