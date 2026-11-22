"""Multi-premise dispatch under distribution-network limits.

NetworkDispatchModel puts every premise on one Pyomo model (each through the same
Premise.register used standalone), adds a flow variable per network element equal to
the net import of the premises below it, and limits that flow to the element's rating.
The objective is the sum of the premises' own costs (their bills and degradation) plus
network costs: overload above a soft rating and the CapacityDeferral value of peak
loading. That's a planner's objective -- it finds the dispatch that's cheapest for
everyone together, which can raise some customers' bills; what each house gives up is
reported, not compensated (that's the settlement layer's job).

The dual of each element's flow definition is the marginal cost of one more kW through
it -- the congestion price (congestion_prices). Summed along a premise's path to the
root, plus the energy price, it's that premise's locational marginal value at the
meter (the dual of its POI balance).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from der_opt.network.topology import Feeder
from der_opt.premise.premise import Premise, solve_single_meter
from der_opt.results.extract import _solver_status, build_dispatch_result
from der_opt.results.schema import DispatchResult
from der_opt.solvers.interface import SolverConfig
from der_opt.solvers.registry import get_solver
from der_opt.time_grid import TimeGrid


def _flow_name(element: str) -> str:
    return f"net_{element}_flow"


# Within hours that cost the same, nothing in a bill says *when* to charge, so a solver
# returns an arbitrary schedule. Real "charge in off-peak" controls start as soon as the
# cheap window opens, so every optimization here breaks ties that way: a negligible
# cost on each imported kWh that grows by this much per hour into the horizon. It's
# left out of every reported cost.
EARLIEST_TIE_BREAK_PER_KWH_HOUR = 1e-5


def _tie_break_expr(premise: Premise, per_kwh_hour: float):
    dt = premise.time_grid.dt_hours
    return sum(per_kwh_hour * t * dt * dt * premise.grid_import[t] for t in premise.time_grid.periods)


class NetworkValueStream(ABC):
    """A cost or value that depends on network flows, not on any one premise."""

    def apply(self, model: pyo.ConcreteModel, network: NetworkDispatchModel) -> None:
        return

    @abstractmethod
    def total_cost_expr(self, model: pyo.ConcreteModel, network: NetworkDispatchModel):
        raise NotImplementedError

    @abstractmethod
    def evaluate_numeric(self, flows: pd.DataFrame, feeder: Feeder, time_grid: TimeGrid) -> float:
        """The same cost for flows from any dispatch (optimal, independent, simulated)."""
        raise NotImplementedError

    @property
    def label(self) -> str:
        return type(self).__name__


class CapacityDeferral(NetworkValueStream):
    """Values keeping an element's peak load down: value_per_kw x (peak flow above
    threshold_kw). With value_per_kw as an avoided-upgrade cost in $/kW-year, this
    treats the run's peak as the year's peak, so it suits a horizon around the
    element's annual peak (a heat wave, for a summer-peaking transformer). Backfeed
    isn't valued."""

    def __init__(self, element: str, value_per_kw: float, threshold_kw: float = 0.0):
        self.element = element
        self.value_per_kw = value_per_kw
        self.threshold_kw = threshold_kw

    @property
    def label(self) -> str:
        return f"{self.element} capacity deferral"

    def apply(self, model, network):
        flow = getattr(model, _flow_name(self.element))
        self.peak_above = pyo.Var(domain=pyo.NonNegativeReals)
        model.add_component(f"net_{self.element}_deferral_peak", self.peak_above)
        model.add_component(
            f"net_{self.element}_deferral_peak_def",
            pyo.Constraint(model.T, rule=lambda _m, t: self.peak_above >= flow[t] - self.threshold_kw),
        )

    def total_cost_expr(self, model, network):
        return self.value_per_kw * self.peak_above

    def evaluate_numeric(self, flows, feeder, time_grid):
        return self.value_per_kw * max(0.0, float(flows[self.element].max()) - self.threshold_kw)


FixedLoads = dict[str, tuple[str, np.ndarray]]  # name -> (element it's attached to, kW per step)


def fixed_below(feeder: Feeder, element: str, fixed: FixedLoads | None) -> np.ndarray | None:
    """Fixed kW through an element from loads attached at or below it (background load,
    non-participants), or None if there are none."""
    total = None
    for attached, kw in (fixed or {}).values():
        if element in feeder.path_to_root(attached):
            total = np.asarray(kw, dtype=float) if total is None else total + np.asarray(kw, dtype=float)
    return total


def element_flows(feeder: Feeder, dispatches: dict[str, DispatchResult], fixed: FixedLoads | None = None,
                  index=None) -> pd.DataFrame:
    """kW through each element (positive toward the premises, negative = backfeed) for
    any set of premise dispatches -- optimized jointly, separately, or simulated -- plus
    any fixed loads (background load; non-participants solved on their own)."""
    net = {name: result.grid_import - result.grid_export for name, result in dispatches.items()}
    covered = set(net) | set(fixed or {})
    missing = set(feeder.attachments) - covered
    if missing:
        raise ValueError(f"no dispatch for attached premises {sorted(missing)}")
    if index is None:
        index = next(iter(net.values())).index
    flows = {}
    for element in feeder.elements:
        below = [p for p in feeder.premises_below(element.name) if p in net]
        flow = sum((net[p] for p in below), pd.Series(0.0, index=index))
        extra = fixed_below(feeder, element.name, {k: v for k, v in (fixed or {}).items() if k not in net})
        flows[element.name] = flow + (extra if extra is not None else 0.0)
    return pd.DataFrame(flows)


def overload_cost(flows: pd.DataFrame, feeder: Feeder, time_grid: TimeGrid) -> float:
    total = 0.0
    for element in feeder.elements:
        if element.rating_kw is None or element.overload_cost_per_kwh is None:
            continue
        over = np.clip(flows[element.name].abs().to_numpy() - element.rating_kw, 0, None)
        total += element.overload_cost_per_kwh * float(over.sum()) * time_grid.dt_hours
    return total


def loading_summary(flows: pd.DataFrame, feeder: Feeder, time_grid: TimeGrid) -> pd.DataFrame:
    """Per element: peak load and backfeed, % of rating, and hours / kWh over rating."""
    rows = []
    for element in feeder.elements:
        flow = flows[element.name].to_numpy()
        row = {
            "element": element.name,
            "rating_kw": element.rating_kw,
            "peak_kw": float(flow.max()),
            "peak_time": flows.index[int(flow.argmax())],
            "max_backfeed_kw": float(max(0.0, -flow.min())),
        }
        if element.rating_kw:
            over = np.clip(np.abs(flow) - element.rating_kw, 0, None)
            row["peak_pct_of_rating"] = 100 * float(np.abs(flow).max()) / element.rating_kw
            row["hours_over"] = float((over > 1e-6).sum() * time_grid.dt_hours)
            row["kwh_over"] = float(over.sum() * time_grid.dt_hours)
        rows.append(row)
    return pd.DataFrame(rows).set_index("element")


@dataclass
class NetworkResult:
    premises: dict[str, DispatchResult]
    flows: pd.DataFrame
    objective_value: float
    solver_status: str
    network_costs: dict[str, float] = field(default_factory=dict)


class NetworkDispatchModel:
    """Every premise and the feeder on one model. All premises must share one
    TimeGrid (same resolution, horizon and start)."""

    def __init__(
        self,
        feeder: Feeder,
        premises: list[Premise],
        value_streams: list[NetworkValueStream] | None = None,
        tie_break_per_kwh_hour: float = EARLIEST_TIE_BREAK_PER_KWH_HOUR,
        fixed_loads: FixedLoads | None = None,
    ):
        """fixed_loads: kW through the network that the optimization can't change --
        background load, and non-participants' own dispatch -- by name, with the element
        each is attached to."""
        names = [premise.name for premise in premises]
        if len(set(names)) != len(names):
            raise ValueError("premise names must be unique on a feeder")
        unattached = set(names) - set(feeder.attachments)
        if unattached:
            raise ValueError(f"premises not attached to any network element: {sorted(unattached)}")
        grids = {premise.time_grid for premise in premises}
        if len(grids) != 1:
            raise ValueError("all premises on a feeder need the same TimeGrid")
        self.feeder = feeder
        self.premises = list(premises)
        self.value_streams = list(value_streams or [])
        self.tie_break_per_kwh_hour = tie_break_per_kwh_hour
        self.time_grid = premises[0].time_grid
        self.fixed_loads = dict(fixed_loads or {})
        self.flows: dict[str, pyo.Var] = {}

    def build_model(self) -> pyo.ConcreteModel:
        model = pyo.ConcreteModel()
        for premise in self.premises:
            premise.register(model, prefix=f"{premise.name}_")
        by_name = {premise.name: premise for premise in self.premises}
        network_cost = 0
        dt = self.time_grid.dt_hours

        for element in self.feeder.elements:
            below = [by_name[name] for name in self.feeder.premises_below(element.name) if name in by_name]
            fixed = fixed_below(self.feeder, element.name, self.fixed_loads)
            fixed = np.zeros(len(model.T)) if fixed is None else fixed
            flow = pyo.Var(model.T, domain=pyo.Reals)
            model.add_component(_flow_name(element.name), flow)
            self.flows[element.name] = flow
            model.add_component(
                f"{_flow_name(element.name)}_def",
                pyo.Constraint(
                    model.T,
                    rule=lambda _m, t, f=flow, ps=below, x=fixed: f[t]
                    == sum(p.grid_import[t] - p.grid_export[t] for p in ps) + float(x[t]),
                ),
            )
            if element.rating_kw is None:
                continue
            rating = element.rating_kw
            if element.overload_cost_per_kwh is None:
                over_load = over_back = {t: 0 for t in model.T}
            else:
                over_load = pyo.Var(model.T, domain=pyo.NonNegativeReals)
                over_back = pyo.Var(model.T, domain=pyo.NonNegativeReals)
                model.add_component(f"net_{element.name}_overload", over_load)
                model.add_component(f"net_{element.name}_overload_backfeed", over_back)
                network_cost += element.overload_cost_per_kwh * dt * sum(over_load[t] + over_back[t] for t in model.T)
            model.add_component(
                f"net_{element.name}_rating",
                pyo.Constraint(model.T, rule=lambda _m, t, f=flow, o=over_load, r=rating: f[t] <= r + o[t]),
            )
            model.add_component(
                f"net_{element.name}_rating_backfeed",
                pyo.Constraint(model.T, rule=lambda _m, t, f=flow, o=over_back, r=rating: f[t] >= -r - o[t]),
            )

        for stream in self.value_streams:
            stream.apply(model, self)
            network_cost += stream.total_cost_expr(model, self)

        self._network_cost_expr = network_cost
        tie_break = sum(_tie_break_expr(p, self.tie_break_per_kwh_hour) for p in self.premises)
        tie_break = tie_break + sum(p.control_cost_expr(model) for p in self.premises)  # smoothing: unreported too
        model.obj = pyo.Objective(
            expr=sum(premise.total_cost_expr(model) for premise in self.premises) + network_cost + tie_break,
            sense=pyo.minimize,
        )
        self._tie_break_expr = tie_break
        return model

    def solve(self, solver_config: SolverConfig | None = None):
        """Returns (NetworkResult, model, backend); see Premise.solve."""
        return PooledNetworkSolver(solver_config).solve(self)

    def result_from_model(self, model: pyo.ConcreteModel, solver_result) -> NetworkResult:
        dispatches = {
            premise.name: build_dispatch_result(
                premise, model, solver_result, objective_value=pyo.value(premise.total_cost_expr(model))
            )
            for premise in self.premises
        }
        flows = element_flows(self.feeder, dispatches, self.fixed_loads)
        return NetworkResult(
            premises=dispatches,
            flows=flows,
            objective_value=pyo.value(model.obj) - pyo.value(self._tie_break_expr),
            solver_status=_solver_status(solver_result),
            network_costs=self.network_costs(flows),
        )

    def network_costs(self, flows: pd.DataFrame) -> dict[str, float]:
        """Overload and network value-stream costs for flows from any dispatch."""
        costs = {"overload": overload_cost(flows, self.feeder, self.time_grid)}
        for stream in self.value_streams:
            costs[stream.label] = stream.evaluate_numeric(flows, self.feeder, self.time_grid)
        return costs

    def congestion_prices(self, model: pyo.ConcreteModel, backend) -> tuple[pd.DataFrame, bool]:
        """$/kWh cost of one more kWh through each element, by period (time x element),
        and whether the duals are exact (False after a MILP's fix-and-resolve)."""
        from der_opt.pricing.duals import extract_duals

        families = [f"{_flow_name(element.name)}_def" for element in self.feeder.elements]
        duals = extract_duals(model, backend, constraint_names=families)
        prices = {}
        exact = True
        for element in self.feeder.elements:
            dual = duals[f"{_flow_name(element.name)}_def"]
            exact = exact and dual.is_exact
            # the dual of flow == (sum of net imports) is the objective's change per kW
            # of flow forced through the element (sign checked against a finite
            # difference in tests/unit/test_network.py); per kWh, divide by the period
            prices[element.name] = dual.values.sort_index().to_numpy() / self.time_grid.dt_hours
        index = pd.DatetimeIndex(self.time_grid.timestamps()) if self.time_grid.start else list(self.time_grid.periods)
        return pd.DataFrame(prices, index=index), exact


class NetworkSolver(ABC):
    """How a NetworkDispatchModel gets solved. Pooled (one model) is the only strategy
    so far; a decomposition (ADMM, Benders) for large feeders would go behind the same
    call."""

    @abstractmethod
    def solve(self, network: NetworkDispatchModel):
        raise NotImplementedError


class PooledNetworkSolver(NetworkSolver):
    def __init__(self, solver_config: SolverConfig | None = None):
        self.solver_config = solver_config or SolverConfig()

    def solve(self, network: NetworkDispatchModel):
        model = network.build_model()
        backend = get_solver(self.solver_config)
        solver_result = solve_single_meter(backend, model, network.premises)
        return network.result_from_model(model, solver_result), model, backend


def solve_independently(
    premises: list[Premise],
    solver_config: SolverConfig | None = None,
    tie_break_per_kwh_hour: float = EARLIEST_TIE_BREAK_PER_KWH_HOUR,
) -> dict[str, DispatchResult]:
    """Each premise optimizing only its own costs, blind to the network -- what the
    feeder sees when every home runs its own optimizer. Pass the result to
    element_flows for the loading."""
    results = {}
    for premise in premises:
        model = premise.build_standalone_model()
        tie_break = _tie_break_expr(premise, tie_break_per_kwh_hour) + premise._control_cost  # neither is reported
        model.obj.expr = model.obj.expr + _tie_break_expr(premise, tie_break_per_kwh_hour)
        solver_result = solve_single_meter(get_solver(solver_config or SolverConfig()), model, [premise])
        results[premise.name] = build_dispatch_result(
            premise, model, solver_result, objective_value=pyo.value(model.obj) - pyo.value(tie_break)
        )
    return results
