from __future__ import annotations

import matplotlib.pyplot as plt

_COST_COLOR = "#d62728"
_REVENUE_COLOR = "#2ca02c"
_TOTAL_COLOR = "#1f77b4"


def value_stack(breakdown: dict[str, float], title: str | None = None, ax=None):
    """Bar chart decomposing total $ value by source (each ValueStream, plus any
    device-level cost like battery/EV degradation) and a Total bar -- answers "where
    did the savings come from." Pass der_opt.pricing.evaluate.value_breakdown's output
    directly. Positive bars (net cost) are red, negative (net revenue) are green."""
    labels = list(breakdown.keys()) + ["Total"]
    values = list(breakdown.values()) + [sum(breakdown.values())]
    colors = [_COST_COLOR if v >= 0 else _REVENUE_COLOR for v in values]
    colors[-1] = _TOTAL_COLOR

    fig = ax.figure if ax is not None else None
    if ax is None:
        fig, ax = plt.subplots(figsize=(max(6, 1.2 * len(labels)), 5))

    ax.bar(labels, values, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("$")
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    if title:
        ax.set_title(title)

    fig.tight_layout()
    return fig
