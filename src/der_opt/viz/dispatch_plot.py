from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

from der_opt.results.schema import DispatchResult


def dispatch_plot(dispatch: DispatchResult, price=None, title: str | None = None, ax=None):
    """Stacked dispatch timeseries: each device's net_power (positive = supplies the
    bus, negative = draws from it) plus grid_import/grid_export, with an optional price
    series overlaid on a secondary axis -- "what did the optimizer (or policy) do and
    why." Positive and negative components are stacked separately above/below zero so
    mixed-sign series (a battery that both charges and discharges over the horizon)
    render legibly."""
    df = dispatch.device_dispatch
    net_power_cols = [c for c in df.columns if c.endswith(".net_power")]
    series = {col[: -len(".net_power")]: df[col].to_numpy() for col in net_power_cols}
    series["grid_import"] = dispatch.grid_import.to_numpy()
    series["grid_export"] = -dispatch.grid_export.to_numpy()

    index = df.index

    fig = ax.figure if ax is not None else None
    if ax is None:
        fig, ax = plt.subplots(figsize=(11, 5))

    positive = {name: np.clip(values, 0, None) for name, values in series.items()}
    negative = {name: np.clip(values, None, 0) for name, values in series.items()}
    pos_names = [name for name, values in positive.items() if np.any(values > 0)]
    neg_names = [name for name, values in negative.items() if np.any(values < 0)]

    if pos_names:
        ax.stackplot(index, *[positive[name] for name in pos_names], labels=pos_names, alpha=0.85)
    if neg_names:
        ax.stackplot(index, *[negative[name] for name in neg_names], labels=neg_names, alpha=0.5)

    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("kW")
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    if title:
        ax.set_title(title)

    if price is not None:
        price_values = price.to_numpy() if hasattr(price, "to_numpy") else np.asarray(price)
        ax2 = ax.twinx()
        ax2.plot(index, price_values, color="black", linestyle="--", linewidth=1.2, label="price")
        ax2.set_ylabel("$/kWh")

    fig.tight_layout()
    return fig
