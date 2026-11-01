from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Any

import pyomo.environ as pyo

from der_opt.pricing.sources import PriceSeries
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


class ValueStream(ABC):
    """A grid-service revenue/cost stream (energy arbitrage, demand charge, capacity
    payment, ...). Adding a service is adding a ValueStream, not touching device code.
    Each method is handed the owning Premise so it can reference that premise's own
    grid_import/grid_export variables rather than a global model-level name -- this is
    what keeps multiple premises' value streams from colliding once they're composed
    onto one shared model (Phase 3)."""

    def apply(self, model: pyo.ConcreteModel, premise: Premise) -> None:
        """Hook for a stream that needs extra vars/constraints (e.g. a demand-charge
        peak variable). Default: no-op."""
        return

    def cost_expr(self, model: pyo.ConcreteModel, premise: Premise, t: int) -> Any:
        """Per-period $ cost contribution (negative = revenue). Default 0; override this
        for a stream whose charge is naturally per-period, or override total_cost_expr
        directly for one that isn't (e.g. a once-per-horizon demand charge)."""
        return 0

    def total_cost_expr(self, model: pyo.ConcreteModel, premise: Premise) -> Any:
        return sum(self.cost_expr(model, premise, t) for t in model.T)


class EnergyArbitrage(ValueStream):
    """Energy import/export cost against two independent, possibly time-varying price
    streams (retail tariff, wholesale LMP, or any other PriceSeries)."""

    def __init__(self, import_price: PriceSeries, export_price: PriceSeries, time_grid: TimeGrid):
        self._import_rates = import_price.values(time_grid)
        self._export_rates = export_price.values(time_grid)

    def cost_expr(self, model: pyo.ConcreteModel, premise: Premise, t: int):
        return (
            self._import_rates[t] * premise.grid_import[t]
            - self._export_rates[t] * premise.grid_export[t]
        )
