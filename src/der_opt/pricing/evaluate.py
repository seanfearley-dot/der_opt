from __future__ import annotations

from typing import TYPE_CHECKING

from der_opt.results.schema import DispatchResult

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


def value_breakdown(premise: Premise, dispatch: DispatchResult) -> dict[str, float]:
    """$ value by source for one DispatchResult (from either Premise.solve() or
    CounterfactualSimulator.run()): one entry per ValueStream (priced via its
    evaluate_numeric against the realized grid_import/export, so a tiered tariff is
    costed the same way whether the dispatch was optimized or simulated), plus one
    entry per device that exposes a per-period "cost" column (battery/EV degradation).
    Powers der_opt.viz.value_stack and der_opt.viz.comparison_plot."""
    grid_import = dispatch.grid_import.to_numpy()
    grid_export = dispatch.grid_export.to_numpy()

    breakdown: dict[str, float] = {}
    for stream in premise.value_streams:
        label = type(stream).__name__
        value = stream.evaluate_numeric(grid_import, grid_export, premise.time_grid)
        breakdown[label] = breakdown.get(label, 0.0) + value

    for device in premise.devices:
        cost_column = f"{device.name}.cost"
        if cost_column in dispatch.device_dispatch.columns:
            breakdown[f"{device.name} degradation"] = float(dispatch.device_dispatch[cost_column].sum())

    return breakdown
