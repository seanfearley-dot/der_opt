from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pyomo.environ as pyo

from der_opt.time_grid import TimeGrid


class Device(ABC):
    """Base class for a resource-specific DER device (solar, battery, thermostat, ...).

    A device never owns its own Pyomo model: it registers variables and constraints onto
    a model handed to it by a Premise. This is what lets a single-premise model and a
    later multi-premise model share device code unmodified (see Premise.register).

    Every device also carries a pure-numeric `step()` alongside its symbolic Pyomo
    constraints, so a ControlPolicy-driven counterfactual simulation can exercise the same
    physics without invoking the solver. The two representations must stay consistent;
    see tests/unit/devices for the consistency checks.
    """

    def __init__(self, name: str, time_grid: TimeGrid):
        self.name = name
        self.time_grid = time_grid

    def _component_name(self, varname: str) -> str:
        return f"{self.name}_{varname}"

    @abstractmethod
    def build_vars(self, model: pyo.ConcreteModel) -> None:
        """Create this device's Pyomo Vars and attach them to the model (namespaced via
        self._component_name) as well as to self, so downstream code can read
        device.some_var[t] directly rather than via string lookup."""
        raise NotImplementedError

    @abstractmethod
    def build_constraints(self, model: pyo.ConcreteModel) -> None:
        """Create this device's Pyomo Constraints (symbolic form of its physics) and
        attach them to the model, namespaced via self._component_name."""
        raise NotImplementedError

    @abstractmethod
    def net_power_expr(self, model: pyo.ConcreteModel, t: int) -> Any:
        """kW this device puts onto the premise bus at period t: +export / -draw."""
        raise NotImplementedError

    def cost_expr(self, model: pyo.ConcreteModel, t: int) -> Any:
        """Any device-intrinsic cost at period t (e.g. degradation). Zero by default."""
        return 0

    def register(self, model: pyo.ConcreteModel) -> None:
        self.build_vars(model)
        self.build_constraints(model)

    def initial_state(self) -> dict[str, Any]:
        """Starting state for the numeric step() path (e.g. {"soc": ...} for a
        battery). Default: no persistent state (solar, curtailable load)."""
        return {}

    @abstractmethod
    def step(self, state: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        """Pure-numeric physics update for one period, evaluated as plain floats — the
        counterfactual/simulation counterpart to build_constraints' symbolic version.
        `state` holds this device's state at the start of the period (e.g. {"soc": ...});
        `action` holds the control decision a ControlPolicy chose for this period (e.g.
        {"p_ch": ..., "p_dis": ...}). Returns the next-period state plus any realized
        quantities a caller needs (e.g. net power), under the same keys build_constraints'
        variables use, so results assembly doesn't need device-specific branching."""
        raise NotImplementedError

    def dispatch_series(self, model: pyo.ConcreteModel) -> dict[str, np.ndarray]:
        """Named arrays of this device's solved variables, for results/viz assembly.
        Default exposes only net power; concrete devices override to add detail (e.g.
        a battery adds p_ch/p_dis/soc)."""
        values = np.array([pyo.value(self.net_power_expr(model, t)) for t in self.time_grid.periods])
        return {"net_power": values}
