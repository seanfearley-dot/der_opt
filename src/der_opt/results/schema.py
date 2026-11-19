from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class DispatchResult:
    """Dispatch output for one solved (or simulated) Premise."""

    premise_name: str
    time_index: pd.DatetimeIndex | None
    device_dispatch: pd.DataFrame  # columns like "battery.p_ch", "battery.soc", ...
    grid_import: pd.Series
    grid_export: pd.Series
    objective_value: float
    solver_status: str
    # each storage device's state after the last period, e.g. {"battery": {"soc": 6.2}}
    # (the dispatch columns hold start-of-period values, so they stop one step short)
    final_state: dict = field(default_factory=dict)


@dataclass
class DualResult:
    """Dual/shadow-price values for one constraint family."""

    constraint_name: str
    values: pd.Series
    is_exact: bool = True
    note: str = field(default="")
