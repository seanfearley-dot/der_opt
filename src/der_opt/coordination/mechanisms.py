"""The mechanisms as a participant sees them (docs/COORDINATION_SPEC.md sections 3-4):
the price signal's adder by step, and the value streams that put a signal and an
envelope into a participant's own optimization."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from der_opt.network.topology import Feeder
from der_opt.premise.objective import ValueStream
from der_opt.time_grid import TimeGrid


def _hours_mask(index: pd.DatetimeIndex, window: str) -> np.ndarray:
    start, end = window.split("-")
    to_minutes = lambda text: int(text.split(":")[0]) * 60 + int(text.split(":")[1])
    lo, hi = to_minutes(start), to_minutes(end)
    minutes = index.hour * 60 + index.minute
    return np.asarray((minutes >= lo) & (minutes < hi)) if lo < hi else np.asarray((minutes >= lo) | (minutes < hi))


def element_prices(config, index: pd.DatetimeIndex, feeder: Feeder, congestion: pd.DataFrame | None) -> pd.DataFrame:
    """$/kWh on each element by step, from the signal's source."""
    if config.source == "coordinated":
        if congestion is None:
            raise ValueError("price_signal source coordinated needs the coordinated run's congestion prices")
        return congestion.reindex(columns=[e.name for e in feeder.elements]).fillna(0.0).set_axis(index)
    prices = pd.DataFrame(0.0, index=index, columns=[e.name for e in feeder.elements])
    if config.source == "schedule":
        for rule in config.schedule:
            if rule.element not in prices:
                raise ValueError(f"price_signal schedule: {rule.element!r} isn't a network element")
            mask = _hours_mask(index, rule.hours)
            if rule.months != "all":
                mask &= np.isin(index.month, rule.months)
            if rule.days == "weekdays":
                mask &= index.day_of_week < 5
            elif rule.days == "weekends":
                mask &= index.day_of_week >= 5
            prices.loc[mask, rule.element] += rule.per_kwh
        return prices
    if config.path is None:
        raise ValueError("price_signal source file needs path")
    frame = pd.read_csv(config.path, encoding="utf-8-sig")
    missing = {"timestamp", "element_id", "price_per_kwh"} - set(frame.columns)
    if missing:
        raise ValueError(f"{config.path}: missing column(s) {sorted(missing)}")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    table = frame.pivot_table(index="timestamp", columns="element_id", values="price_per_kwh").reindex(index)
    unknown = set(table.columns) - set(prices.columns)
    if unknown:
        raise ValueError(f"{config.path}: unknown element(s) {sorted(unknown)}")
    for element in table.columns:
        prices[element] = table[element].fillna(0.0).to_numpy()
    return prices


def participant_adder(prices: pd.DataFrame, feeder: Feeder, premise: str) -> np.ndarray:
    """The adder a participant pays per kWh of net import: the sum along its path."""
    path = feeder.path_to_root(feeder.attachments[premise])
    return prices[path].sum(axis=1).to_numpy(dtype=float)


class SignalAdder(ValueStream):
    """adder[t] $/kWh on net import (import - export): paid at peaks, earned exporting
    into them."""

    def __init__(self, adder: np.ndarray, time_grid: TimeGrid):
        self.rates = np.asarray(adder, dtype=float) * time_grid.dt_hours

    def cost_expr(self, model, premise, t):
        return float(self.rates[t]) * (premise.grid_import[t] - premise.grid_export[t])

    def evaluate_numeric(self, grid_import, grid_export, time_grid):
        return float(np.sum(self.rates * (np.asarray(grid_import) - np.asarray(grid_export))))

    def pass_through_gain(self, premise):
        return np.zeros(premise.time_grid.horizon_steps)  # net import only


class EnvelopeLimit(ValueStream):
    """Import and export no more than the envelope, softly: each kWh over costs the
    penalty (so a load the participant can't move doesn't make the run unsolvable)."""

    def __init__(self, import_caps, export_caps, penalty_per_kwh: float, time_grid: TimeGrid):
        self.import_caps = None if import_caps is None else np.asarray(import_caps, dtype=float)
        self.export_caps = None if export_caps is None else np.asarray(export_caps, dtype=float)
        self.penalty = penalty_per_kwh * time_grid.dt_hours

    def _steps(self, caps):
        return [] if caps is None else [int(t) for t in np.flatnonzero(np.isfinite(caps))]

    def apply(self, model, premise):
        self.over = {}
        for quantity, caps, flow in (("import", self.import_caps, premise.grid_import),
                                     ("export", self.export_caps, premise.grid_export)):
            steps = self._steps(caps)
            if not steps:
                continue
            over = pyo.Var(steps, domain=pyo.NonNegativeReals)
            model.add_component(f"{premise.name}_envelope_{quantity}_over", over)
            model.add_component(f"{premise.name}_envelope_{quantity}",
                                pyo.Constraint(steps, rule=lambda _m, t, f=flow, c=caps, o=over: f[t] <= float(c[t]) + o[t]))
            self.over[quantity] = over

    def total_cost_expr(self, model, premise):
        return sum(self.penalty * sum(over[t] for t in over) for over in self.over.values())

    def evaluate_numeric(self, grid_import, grid_export, time_grid):
        return self.penalty * float(sum(violations(self.import_caps, self.export_caps, grid_import, grid_export)))

    def pass_through_gain(self, premise):
        return np.zeros(premise.time_grid.horizon_steps)  # more of either can only cost


def violations(import_caps, export_caps, grid_import, grid_export) -> tuple[float, float]:
    """kW-steps over each envelope (multiply by dt for kWh)."""
    over_import = 0.0 if import_caps is None else float(np.clip(
        np.asarray(grid_import) - np.where(np.isfinite(import_caps), import_caps, np.inf), 0, None).sum())
    over_export = 0.0 if export_caps is None else float(np.clip(
        np.asarray(grid_export) - np.where(np.isfinite(export_caps), export_caps, np.inf), 0, None).sum())
    return over_import, over_export
