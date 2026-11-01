from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib.pyplot as plt
import numpy as np

from der_opt.pricing.evaluate import value_breakdown
from der_opt.results.schema import DispatchResult

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


def comparison_plot(
    optimal: DispatchResult,
    counterfactual: DispatchResult,
    premise: Premise | None = None,
    title: str | None = None,
):
    """Overlays an optimal-dispatch DispatchResult against a counterfactual one -- the
    "value of optimization vs. realistic baseline behavior" chart. Top panel: grid
    import/export timeseries for both. Bottom panel (only if `premise` is given, so the
    $ value can be priced via value_breakdown): a side-by-side bar comparison of value
    by source for the two dispatches."""
    index = optimal.device_dispatch.index

    if premise is not None:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 10))
    else:
        fig, ax1 = plt.subplots(figsize=(11, 5))
        ax2 = None

    ax1.plot(index, optimal.grid_import.to_numpy(), color="#1f77b4", label="optimal import")
    ax1.plot(
        index, counterfactual.grid_import.to_numpy(), color="#ff7f0e", linestyle="--",
        label="counterfactual import",
    )
    ax1.plot(index, -optimal.grid_export.to_numpy(), color="#1f77b4", alpha=0.5, label="optimal export")
    ax1.plot(
        index, -counterfactual.grid_export.to_numpy(), color="#ff7f0e", alpha=0.5, linestyle="--",
        label="counterfactual export",
    )
    ax1.axhline(0, color="black", linewidth=0.6)
    ax1.set_ylabel("kW (+ import / - export)")
    ax1.legend(fontsize=8)
    ax1.set_title(title or "Optimal vs. counterfactual dispatch")

    if ax2 is not None:
        optimal_breakdown = value_breakdown(premise, optimal)
        cf_breakdown = value_breakdown(premise, counterfactual)
        labels = sorted(set(optimal_breakdown) | set(cf_breakdown))
        x = np.arange(len(labels))
        width = 0.35
        ax2.bar(x - width / 2, [optimal_breakdown.get(label, 0.0) for label in labels], width, label="optimal")
        ax2.bar(
            x + width / 2, [cf_breakdown.get(label, 0.0) for label in labels], width, label="counterfactual",
        )
        ax2.set_xticks(x)
        ax2.set_xticklabels(labels, rotation=30, ha="right")
        ax2.axhline(0, color="black", linewidth=0.6)
        ax2.set_ylabel("$")
        ax2.legend(fontsize=8)
        ax2.set_title("Value breakdown: optimal vs. counterfactual")

    fig.tight_layout()
    return fig
