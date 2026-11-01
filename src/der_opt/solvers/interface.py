from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyomo.environ as pyo


@dataclass
class SolverConfig:
    """Picks a solver backend without touching any model code. Swapping to Gurobi/CPLEX
    later is `SolverConfig(name="gurobi")` — nothing else changes."""

    name: str = "highs"
    use_persistent: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    time_limit_s: float | None = None


class SolverBackend:
    """Wraps a specific solver so model code only ever depends on this interface.

    Dual access is deliberately a separate method rather than always going through a
    `model.dual` Suffix: pyomo's appsi persistent interfaces (used here for HiGHS) expose
    duals via a `get_duals()` accessor on the solver object instead of populating a
    Suffix, while the classic SolverFactory path (used for CBC/Gurobi/CPLEX) does use a
    Suffix. get_duals() below hides that difference behind one call.
    """

    def __init__(self, config: SolverConfig):
        self.config = config

    def solve(self, model: pyo.ConcreteModel) -> Any:
        raise NotImplementedError

    def get_duals(self, model: pyo.ConcreteModel) -> dict[Any, float]:
        """Maps each active ConstraintData solved in the last solve() call to its dual
        value. Only meaningful for a continuous LP (or LP relaxation) — see
        der_opt.pricing.duals for the MILP fix-and-resolve handling."""
        raise NotImplementedError
