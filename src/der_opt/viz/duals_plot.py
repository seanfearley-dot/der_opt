from __future__ import annotations

import matplotlib.pyplot as plt

from der_opt.results.schema import DualResult


def duals_plot(duals: dict[str, DualResult], names: list[str] | None = None, title: str | None = None, ax=None):
    """Timeseries of selected dual/shadow-price families (e.g. the energy-balance,
    comfort-band, and capacity-constraint duals) -- the marginal-value story central to
    the dual-representation goal. Pass der_opt.pricing.duals.extract_duals's output
    directly; any family whose DualResult.is_exact is False gets a note on the chart."""
    names = names or list(duals.keys())

    fig = ax.figure if ax is not None else None
    if ax is None:
        fig, ax = plt.subplots(figsize=(11, 5))

    any_inexact = False
    for name in names:
        dual = duals[name]
        ax.plot(dual.values.index, dual.values.to_numpy(), marker="o", markersize=3, label=name)
        any_inexact = any_inexact or not dual.is_exact

    ax.axhline(0, color="black", linewidth=0.6)
    ax.set_ylabel("dual value ($/unit)")
    ax.legend(fontsize=8)
    if title:
        ax.set_title(title)
    if any_inexact:
        ax.text(
            0.01, 0.01,
            "note: some duals shown are fix-and-resolve approximations, not exact",
            transform=ax.transAxes, fontsize=7, color="gray",
        )

    fig.tight_layout()
    return fig
