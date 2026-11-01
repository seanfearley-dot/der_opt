from __future__ import annotations

import pyomo.environ as pyo

from der_opt.solvers.interface import SolverBackend, SolverConfig


class LegacySolverBackend(SolverBackend):
    """Any solver reachable via the classic pyo.SolverFactory interface (cbc, gurobi,
    cplex, or highs via its non-persistent plugin). Duals come from a `model.dual`
    Suffix, which this backend attaches automatically if the model doesn't already have
    one (it must exist before solve() for the solver/writer to populate it)."""

    def solve(self, model: pyo.ConcreteModel):
        if not hasattr(model, "dual"):
            model.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
        opt = pyo.SolverFactory(self.config.name)
        options = dict(self.config.options)
        if self.config.time_limit_s is not None:
            options.setdefault("time_limit", self.config.time_limit_s)
        return opt.solve(model, tee=False, options=options)

    def get_duals(self, model: pyo.ConcreteModel) -> dict:
        duals = {}
        for con in model.component_objects(pyo.Constraint, active=True):
            for index in con:
                data = con[index]
                if data in model.dual:
                    duals[data] = model.dual[data]
        return duals


class AppsiHighsBackend(SolverBackend):
    """Persistent HiGHS interface via pyomo.contrib.appsi. Preferred over the plain
    SolverFactory('highs') plugin: supports .update() for rolling-horizon re-solves
    instead of rebuilding the full model. Unlike the legacy path, duals are read via the
    solver object's own get_duals() accessor, not a model.dual Suffix — confirmed against
    the installed pyomo/highspy versions rather than assumed."""

    def __init__(self, config: SolverConfig):
        super().__init__(config)
        self._opt = None

    def solve(self, model: pyo.ConcreteModel):
        from pyomo.contrib.appsi.solvers.highs import Highs

        opt = Highs()
        for key, value in self.config.options.items():
            setattr(opt.config, key, value)
        if self.config.time_limit_s is not None:
            opt.config.time_limit = self.config.time_limit_s
        self._opt = opt
        return opt.solve(model)

    def get_duals(self, model: pyo.ConcreteModel) -> dict:
        if self._opt is None:
            raise RuntimeError("solve() must be called before get_duals()")
        return self._opt.get_duals()


_BACKENDS: dict[str, type[SolverBackend]] = {
    "cbc": LegacySolverBackend,
    "gurobi": LegacySolverBackend,
    "cplex": LegacySolverBackend,
}


def get_solver(config: SolverConfig) -> SolverBackend:
    if config.name == "highs":
        return AppsiHighsBackend(config) if config.use_persistent else LegacySolverBackend(config)
    try:
        backend_cls = _BACKENDS[config.name]
    except KeyError as exc:
        raise ValueError(f"Unknown solver {config.name!r}") from exc
    return backend_cls(config)
