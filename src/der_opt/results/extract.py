from __future__ import annotations

import pandas as pd
import pyomo.environ as pyo

from der_opt.results.schema import DispatchResult


def _solver_status(solver_result) -> str:
    if hasattr(solver_result, "termination_condition"):
        return str(solver_result.termination_condition)
    try:
        return str(solver_result.solver.termination_condition)
    except AttributeError:
        return str(solver_result)


def build_dispatch_result(premise, model: pyo.ConcreteModel, solver_result) -> DispatchResult:
    periods = list(premise.time_grid.periods)
    try:
        time_index = pd.DatetimeIndex(premise.time_grid.timestamps())
    except ValueError:
        time_index = None
    index = time_index if time_index is not None else periods

    device_columns = {}
    for device in premise.devices:
        for key, values in device.dispatch_series(model).items():
            device_columns[f"{device.name}.{key}"] = values
    device_dispatch = pd.DataFrame(device_columns, index=index)

    grid_import = pd.Series([pyo.value(premise.grid_import[t]) for t in periods], index=index)
    grid_export = pd.Series([pyo.value(premise.grid_export[t]) for t in periods], index=index)

    return DispatchResult(
        premise_name=premise.name,
        time_index=time_index,
        device_dispatch=device_dispatch,
        grid_import=grid_import,
        grid_export=grid_export,
        objective_value=pyo.value(model.obj),
        solver_status=_solver_status(solver_result),
    )
