from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd


def interval_heatmap(series: pd.Series, title: str | None = None, cmap: str = "viridis", ax=None):
    """Hour-of-day x day heatmap for any interval series (load, solar, price, ...) --
    the standard way to see TOU/seasonal patterns before and after optimizing. Works at
    any sub-daily resolution (hourly, 15-min, ...): rows are time-of-day, columns are
    calendar date."""
    if not isinstance(series.index, pd.DatetimeIndex):
        raise ValueError("interval_heatmap requires a Series indexed by a DatetimeIndex")

    frame = series.rename("value").to_frame()
    frame["date"] = series.index.date
    frame["time_of_day"] = series.index.strftime("%H:%M")
    pivot = frame.pivot_table(index="time_of_day", columns="date", values="value").sort_index()

    fig = ax.figure if ax is not None else None
    if ax is None:
        fig, ax = plt.subplots(figsize=(min(1 + len(pivot.columns) * 0.5, 14), 6))

    image = ax.imshow(pivot.to_numpy(), aspect="auto", cmap=cmap, origin="lower")

    max_yticks = 24
    y_step = max(1, len(pivot.index) // max_yticks)
    ax.set_yticks(range(0, len(pivot.index), y_step))
    ax.set_yticklabels(pivot.index[::y_step], fontsize=7)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([str(d) for d in pivot.columns], rotation=45, ha="right", fontsize=7)
    ax.set_xlabel("Date")
    ax.set_ylabel("Time of day")
    if title:
        ax.set_title(title)

    fig.colorbar(image, ax=ax, label=series.name or "value")
    fig.tight_layout()
    return fig
