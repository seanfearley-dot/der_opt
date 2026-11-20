"""How a measure responds to DR events inside the optimization (spec section 3).

The optimizer is paid, or bound, against an *expected* settlement baseline: the
settlement rule applied to the non-responding twin (R2). The rule applied to the
responding run's own history can't sit inside the optimization: it picks days by their
load (not linear) and scales by a ratio (not linear). With perfect foresight the
optimizer could also shape non-event days to raise its own baseline. The study reports
the settlement actually earned, from the responding run's own history, next to this.

  incentive    pays incentive_per_kwh x (baseline - load) in each event step.
               Linear, so a step above the baseline costs the same rate; the reported
               settlement floors each event's payment at zero.
  commitment   load <= baseline - committed_kw + shortfall, shortfall penalized
  import_cap   import <= cap_kw + shortfall, shortfall penalized

"Load" follows the program's settlement_basis: `net` (import - export, so exporting
counts as reduction) or `import` (grid import only, so reduction stops at zero import
and exporting earns nothing). The baseline handed in must be on the same basis.
"""

from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.premise.objective import ValueStream
from der_opt.time_grid import TimeGrid


class DREventResponse(ValueStream):
    def __init__(self, program, event_mask: np.ndarray, expected_baseline: np.ndarray, time_grid: TimeGrid):
        self.program = program
        self.response = program.response
        self.mask = np.asarray(event_mask, dtype=bool)
        self.baseline = np.nan_to_num(np.asarray(expected_baseline, dtype=float))
        self.dt = time_grid.dt_hours
        if self.response == "incentive" and program.incentive_per_kwh is None:
            raise ValueError("response incentive needs incentive_per_kwh")
        if self.response == "commitment" and program.committed_kw is None:
            raise ValueError("response commitment needs committed_kw")
        if self.response == "import_cap" and program.cap_kw is None:
            raise ValueError("response import_cap needs cap_kw")
        self.steps = [int(t) for t in np.flatnonzero(self.mask)]
        self.basis = getattr(program, "settlement_basis", "net")

    def _load(self, premise, t):
        if self.basis == "import":
            return premise.grid_import[t]
        return premise.grid_import[t] - premise.grid_export[t]

    def apply(self, model, premise):
        if self.response == "incentive" or not self.steps:
            return
        self.shortfall = pyo.Var(self.steps, domain=pyo.NonNegativeReals)
        model.add_component(f"{premise.name}_dr_shortfall", self.shortfall)
        if self.response == "commitment":
            rule = lambda _m, t: self._load(premise, t) <= self.baseline[t] - self.program.committed_kw + self.shortfall[t]
        else:
            rule = lambda _m, t: premise.grid_import[t] <= self.program.cap_kw + self.shortfall[t]
        model.add_component(f"{premise.name}_dr_obligation", pyo.Constraint(self.steps, rule=rule))

    def total_cost_expr(self, model, premise):
        if not self.steps:
            return 0
        if self.response == "incentive":
            rate = self.program.incentive_per_kwh * self.dt
            return -sum(rate * (self.baseline[t] - self._load(premise, t)) for t in self.steps)
        penalty = self.program.shortfall_penalty_per_kwh * self.dt
        return sum(penalty * self.shortfall[t] for t in self.steps)

    def evaluate_numeric(self, grid_import, grid_export, time_grid):
        imp, exp = np.asarray(grid_import), np.asarray(grid_export)
        load = imp if self.basis == "import" else imp - exp
        m = self.mask
        if self.response == "incentive":
            return float(-self.program.incentive_per_kwh * self.dt * (self.baseline[m] - load[m]).sum())
        if self.response == "commitment":
            short = np.clip(load[m] - (self.baseline[m] - self.program.committed_kw), 0, None)
        else:
            short = np.clip(imp[m] - self.program.cap_kw, 0, None)
        return float(self.program.shortfall_penalty_per_kwh * self.dt * short.sum())
