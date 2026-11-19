from __future__ import annotations

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from der_opt.network.topology import Feeder

# reference categorical palette, slots 1-3 in order; text and grid in neutral ink
_RUN_COLORS = {"coordinated": "#2a78d6", "independent": "#eb6834", "counterfactual": "#1baf7a"}
_FALLBACK = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
_SURFACE, _TEXT, _MUTED, _GRID = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9"


def feeder_loading_plot(flows_by_run: dict[str, pd.DataFrame], feeder: Feeder, title: str | None = None):
    """One panel per network element: kW through it under each run, with its rating.
    Each panel has its own scale (a lateral carries far more than one transformer)."""
    elements = feeder.elements
    fig, axes = plt.subplots(len(elements), 1, figsize=(11, 2.8 * len(elements)), sharex=True, squeeze=False)
    fig.patch.set_facecolor(_SURFACE)
    for ax, element in zip(axes[:, 0], elements):
        ax.set_facecolor(_SURFACE)
        for i, (label, flows) in enumerate(flows_by_run.items()):
            color = _RUN_COLORS.get(label, _FALLBACK[i % len(_FALLBACK)])
            ax.plot(flows.index, flows[element.name], color=color, linewidth=2, label=label)
        if element.rating_kw is not None:
            ax.axhline(element.rating_kw, color=_MUTED, linewidth=1.2, linestyle=(0, (4, 3)))
            ax.annotate(f"rating {element.rating_kw:g} kW", xy=(1, element.rating_kw), xycoords=("axes fraction", "data"),
                        xytext=(-4, 4), textcoords="offset points", ha="right", va="bottom", fontsize=8, color=_MUTED)
        ax.axhline(0, color=_MUTED, linewidth=0.8)
        ax.set_title(element.name, loc="left", fontsize=10, color=_TEXT)
        ax.set_ylabel("kW", color=_MUTED, fontsize=9)
        ax.grid(axis="y", color=_GRID, linewidth=0.6)
        ax.tick_params(colors=_MUTED, labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(_GRID)
        ax.set_ylim(bottom=min(0, ax.get_ylim()[0]))
    last = axes[-1, 0]
    if isinstance(next(iter(flows_by_run.values())).index, pd.DatetimeIndex):
        last.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=len(labels), frameon=False, fontsize=9, labelcolor=_TEXT)
    if title:
        fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=_TEXT)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig
