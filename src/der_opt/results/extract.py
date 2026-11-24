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


def build_dispatch_result(premise, model: pyo.ConcreteModel, solver_result, objective_value: float | None = None) -> DispatchResult:
    """objective_value defaults to the model's objective; a premise inside a shared
    (multi-premise) model passes its own share instead."""
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

    end = premise.time_grid.horizon_steps
    final_state = {
        device.name: {"soc": float(pyo.value(device.soc[end]))}
        for device in premise.devices
        if isinstance(getattr(device, "soc", None), pyo.Var)
    }

    return DispatchResult(
        premise_name=premise.name,
        time_index=time_index,
        device_dispatch=device_dispatch,
        grid_import=grid_import,
        grid_export=grid_export,
        objective_value=pyo.value(model.obj) if objective_value is None else objective_value,
        solver_status=_solver_status(solver_result),
        final_state=final_state,
    )


def ev_departures_table(premises, runs: dict[str, dict]) -> pd.DataFrame:
    """Every targeted EV departure in every run (docs/EV_SHORTFALL_SPEC.md section 4):
    `runs` maps a run's label to {premise name: DispatchResult}."""
    rows = []
    for premise in premises:
        for device in premise.devices:
            if not getattr(device, "targeted", None):
                continue
            for label, dispatches in runs.items():
                dispatch = dispatches.get(premise.name)
                if dispatch is None:
                    continue
                for row in device.departures(dispatch):
                    rows.append({"premise_id": premise.name, "device": device.name, "run": label, **row})
    columns = ["premise_id", "device", "run", "departure", "target_kwh", "floor_kwh", "stored_kwh", "short_kwh",
               "shortfall_cost", "shadow_price_per_kwh"]
    return pd.DataFrame(rows, columns=columns)
