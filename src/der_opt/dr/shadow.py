"""Counted capacity from shadow events (spec section 5): for each eligible day, a
small separate optimization asks how far the measure could cut load (net load and
grid import) over a standard event, starting from the non-responding twin's (R2) device state that
morning. Shadow events never touch the annual runs."""

from __future__ import annotations

import copy

import numpy as np
import pyomo.environ as pyo

from der_opt.devices import (
    BatteryStorage,
    CurtailableLoad,
    SmartThermostat,
    SolarPV,
    V2XVehicle,
)
from der_opt.premise import Premise
from der_opt.solvers.interface import SolverConfig
from der_opt.solvers.registry import get_solver
from der_opt.time_grid import TimeGrid

SUPPORTED = (BatteryStorage, CurtailableLoad, SmartThermostat, SolarPV, V2XVehicle)
PRE_EVENT_IMPORT_COST = 1e-4  # $/kWh: don't import before the event without a reason
HELD_TIE_BREAK = 1e-3  # uniform delivery: the total cut, a distant second to the held kW


def slice_premise(premise: Premise, start: int, steps: int, state: dict[str, dict]) -> Premise:
    """The premise from step `start` for `steps` steps, devices starting in `state`."""
    horizon = premise.time_grid.horizon_steps
    stamps = premise.time_grid.timestamps()
    grid = TimeGrid(premise.time_grid.resolution_minutes, steps, stamps[start])
    copies = {}
    for device in premise.devices:
        if not isinstance(device, SUPPORTED):
            raise NotImplementedError(f"shadow events don't support {type(device).__name__} yet")
        d = copy.copy(device)
        d.time_grid, d.namespace = grid, ""
        for attr, value in vars(device).items():
            if isinstance(value, np.ndarray) and value.ndim == 1 and len(value) == horizon:
                setattr(d, attr, value[start:start + steps].copy())
        if isinstance(d, (BatteryStorage, V2XVehicle)):
            d.soc_init = float(state[device.name]["soc"])
            d.soc_final_min = None
        if isinstance(d, V2XVehicle):
            kept = [(p - start, m) for p, m in zip(device.departure_periods, device.soc_departure_mins, strict=False)
                    if start < p <= start + steps]
            d.departure_periods = [p for p, _ in kept]
            d.soc_departure_mins = [m for _, m in kept]
        if isinstance(d, SmartThermostat):
            d.initial_temp_c = float(state[device.name]["temp"])
        copies[device.name] = d
    for d in copies.values():
        if getattr(d, "charge_sources", None):
            d.charge_sources = [copies[src.name] for src in d.charge_sources]
    return Premise(
        f"{premise.name}_shadow", grid, list(copies.values()), [],
        external_load=premise.external_load[start:start + steps],
        import_limit_kw=premise.import_limit_kw, export_limit_kw=premise.export_limit_kw,
    )


def shadow_event(premise: Premise, r2_dispatch, day_start: int, block_start: int, block_steps: int,
                 solver_config: SolverConfig | None = None, floor_on: tuple | None = None) -> np.ndarray:
    """Net load (kW) over the block when the measure cuts as far as it can, starting
    the day from R2's state. Steps run from the day's first step to the block's end.

    `floor_on` = (reference kW over the block, basis "net" or "import"), for uniform
    delivery (spec v1.3): the cut held in every step of the block below the reference
    comes first, and the total cut only breaks ties."""
    frame = r2_dispatch.device_dispatch
    state = {}
    for device in premise.devices:
        entry = {}
        for key in ("soc", "temp"):
            column = f"{device.name}.{key}"
            if column in frame:
                entry[key] = frame[column].iloc[day_start]
        state[device.name] = entry
    steps = block_start + block_steps - day_start
    day = slice_premise(premise, day_start, steps, state)
    model = pyo.ConcreteModel()
    day.register(model)
    dt = day.time_grid.dt_hours
    block = range(block_start - day_start, steps)
    before = range(0, block_start - day_start)
    # minimizing net load alone is indifferent between covering the home's load and
    # exporting; counting imports a second time makes it cover load first, so the one
    # solve gives the most reduction on both bases (net, and import only)
    total = sum((2 * day.grid_import[t] - day.grid_export[t]) * dt for t in block)
    held = 0
    if floor_on is not None:
        reference, basis = floor_on
        model.held = pyo.Var(domain=pyo.Reals)
        load = (lambda t: day.grid_import[t]) if basis == "import" else (
            lambda t: day.grid_import[t] - day.grid_export[t])
        model.uniform = pyo.Constraint(block, rule=lambda _m, t: model.held <= reference[t - block.start] - load(t))
        held = -len(block) * dt * model.held
        total = HELD_TIE_BREAK * total
    model.obj = pyo.Objective(
        expr=held + total + PRE_EVENT_IMPORT_COST * sum(day.grid_import[t] * dt for t in before),
        sense=pyo.minimize,
    )
    get_solver(solver_config or SolverConfig()).solve(model)
    return np.array([pyo.value(day.grid_import[t]) - pyo.value(day.grid_export[t]) for t in block])
