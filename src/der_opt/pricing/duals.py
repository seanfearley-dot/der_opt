from __future__ import annotations

import pandas as pd
import pyomo.environ as pyo

from der_opt.results.schema import DualResult
from der_opt.solvers.interface import SolverBackend

_FIX_AND_RESOLVE_NOTE = (
    "Model had active integer/binary variables; duals were computed by fixing them at "
    "their optimal values and re-solving the resulting continuous LP (fix-and-resolve). "
    "These are not exact Lagrange multipliers of the original MILP."
)


def _has_active_integer_vars(model: pyo.ConcreteModel) -> bool:
    return any(
        var.is_binary() or var.is_integer()
        for var in model.component_data_objects(pyo.Var, active=True)
    )


def _fix_and_resolve(model: pyo.ConcreteModel, backend: SolverBackend):
    # Fixing the *value* isn't enough: HiGHS (and most MIP solvers) only report duals
    # for a solve with no declared integer/binary variables, regardless of whether
    # they're fixed. The domain itself has to be relaxed to continuous for the re-solve.
    fixed_vars = [
        var
        for var in model.component_data_objects(pyo.Var, active=True)
        if var.is_binary() or var.is_integer()
    ]
    original_domains = [var.domain for var in fixed_vars]
    for var in fixed_vars:
        value = pyo.value(var)
        var.domain = pyo.Reals
        var.fix(value)
    try:
        backend.solve(model)
        duals = backend.get_duals(model)
    finally:
        for var, domain in zip(fixed_vars, original_domains):
            var.unfix()
            var.domain = domain
    return duals, _FIX_AND_RESOLVE_NOTE


def extract_duals(
    model: pyo.ConcreteModel,
    backend: SolverBackend,
    constraint_names: list[str] | None = None,
) -> dict[str, DualResult]:
    """Extracts duals for the named constraint families (or all, if None), grouped and
    returned as one DualResult per family. If the model has active integer/binary
    variables (e.g. CurtailableLoad's block variables), re-solves with them fixed at
    their optimal values and pulls duals from that LP instead -- see fix-and-resolve
    above -- flagging DualResult.is_exact=False with an explanatory note."""
    is_exact = not _has_active_integer_vars(model)
    if is_exact:
        duals_by_constraint = backend.get_duals(model)
        note = ""
    else:
        duals_by_constraint, note = _fix_and_resolve(model, backend)

    grouped: dict[str, dict] = {}
    for constraint_data, value in duals_by_constraint.items():
        family = constraint_data.parent_component().name
        grouped.setdefault(family, {})[constraint_data.index()] = value

    if constraint_names is not None:
        grouped = {name: grouped[name] for name in constraint_names if name in grouped}

    results: dict[str, DualResult] = {}
    for family, index_to_value in grouped.items():
        try:
            series = pd.Series(index_to_value).sort_index()
        except TypeError:
            series = pd.Series(index_to_value)
        results[family] = DualResult(
            constraint_name=family, values=series, is_exact=is_exact, note=note
        )
    return results
