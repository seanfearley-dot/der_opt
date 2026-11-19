from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.premise.tariff import TariffSchedule


class PriceSeries(ABC):
    """A per-period $/kWh price array. The seam that lets retail tariffs, wholesale LMP,
    or any other price source plug into EnergyArbitrage (or anything else) interchangeably.
    Wholesale LMP comes in as a CSVPriceSeries; see der_opt.integrations.caiso_lmp for
    the conversion."""

    @abstractmethod
    def values(self, time_grid: TimeGrid) -> np.ndarray:
        raise NotImplementedError


class ConstantPriceSeries(PriceSeries):
    def __init__(self, price: float):
        self.price = price

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        return np.full(time_grid.horizon_steps, self.price, dtype=float)


class DataGapWarning(UserWarning):
    """Input data had to be filled or reshaped to fit the run's time steps. Raised as a
    Python warning so it surfaces wherever the data is read; der-opt run collects these
    into its checks."""


class CSVPriceSeries(PriceSeries):
    """Wraps a user-provided series: a plain array already aligned to the TimeGrid, or a
    pandas Series indexed by timestamp, fitted onto the TimeGrid:

      - data finer than the step (15-min load, hourly run) is averaged over each step
      - data coarser than the step is held across it (hourly price, 15-min run)
      - steps the data skips take the last value before them, with a DataGapWarning
        saying how much was filled, so a missing week never passes silently
      - duplicate timestamps are averaged, with a warning

    `label` names the data in warnings (the loader passes "path [column]")."""

    def __init__(self, series, label: str = "CSV series"):
        self._series = series
        self.label = label

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if isinstance(self._series, pd.Series):
            return self._fit_timestamped(time_grid)

        arr = np.asarray(self._series, dtype=float)
        if arr.shape[0] != time_grid.horizon_steps:
            raise ValueError(
                f"CSVPriceSeries array length {arr.shape[0]} does not match "
                f"time_grid.horizon_steps {time_grid.horizon_steps}"
            )
        return arr

    def _fit_timestamped(self, time_grid: TimeGrid) -> np.ndarray:
        import warnings

        if time_grid.start is None:
            raise ValueError("TimeGrid.start is required to align a timestamp-indexed CSVPriceSeries")
        series = self._series.astype(float).sort_index()
        if getattr(series.index, "tz", None) is not None:
            raise ValueError(
                f"{self.label}: timestamps carry a time zone; convert them to the scenario's local "
                "clock and drop the zone (see der_opt.integrations.caiso_lmp for an example)"
            )
        if series.index.has_duplicates:
            count = int(series.index.duplicated().sum())
            warnings.warn(DataGapWarning(f"{self.label}: {count} duplicate timestamps, averaged"), stacklevel=3)
            series = series.groupby(level=0).mean()

        grid = pd.DatetimeIndex(time_grid.timestamps())
        step = pd.Timedelta(minutes=time_grid.resolution_minutes)
        native = series.index.to_series().diff().median() if len(series) > 1 else step
        if native < step:
            # average each step's worth of finer data; empty steps stay missing
            series = series.resample(step, origin=grid[0]).mean().dropna()
            native = step

        fitted = series.reindex(grid, method="ffill")
        if fitted.isna().any():
            raise ValueError(f"{self.label} does not cover the full TimeGrid horizon (starts {series.index[0]})")

        # how old is the value each step got? A present sample is younger than one
        # source interval (0 when aligned); a full interval or more means the data
        # skipped that step and the previous value was repeated.
        used = series.index[series.index.searchsorted(grid, side="right") - 1]
        age = grid - used
        stale = np.asarray(age >= native * 0.999)
        if stale.any():
            hours = stale.sum() * time_grid.dt_hours
            first = grid[stale.argmax()]
            warnings.warn(DataGapWarning(
                f"{self.label}: {int(stale.sum())} of {len(grid)} steps ({hours:g} h) have no data and "
                f"repeat the last value before them (first at {first}, longest gap {age.max()})"
            ), stacklevel=3)
        return fitted.to_numpy(dtype=float)


class DailyTOUPriceSeries(PriceSeries):
    """Repeats a 24-value hour-of-day rate pattern across the whole TimeGrid horizon --
    an illustrative/placeholder TOU shape (e.g. "cheap overnight, expensive 4-9pm"),
    not a real tariff. Several of this toolkit's own examples hand-rolled exactly this
    pattern inline (`[rate_for_hour(ts.hour) for ts in time_grid.timestamps()]`);
    exposed here as a reusable PriceSeries so a YAML scenario can express it directly
    without a CSV file or external computation. Hourly resolution only."""

    def __init__(self, hourly_pattern):
        pattern = list(hourly_pattern)
        if len(pattern) != 24:
            raise ValueError(f"hourly_pattern must have exactly 24 values, got {len(pattern)}")
        self.hourly_pattern = pattern

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if time_grid.start is None:
            raise ValueError("TimeGrid.start is required for DailyTOUPriceSeries")
        if time_grid.resolution_minutes != 60:
            raise ValueError("DailyTOUPriceSeries only supports hourly (resolution_minutes=60) TimeGrids")
        return np.array([self.hourly_pattern[ts.hour] for ts in time_grid.timestamps()], dtype=float)


class TariffPriceSeries(PriceSeries):
    """Wraps a non-tiered TariffSchedule's TOU energy rate into a per-period array.
    Raises if the tariff has tiers — a tiered rate is decision-dependent (which tier
    applies depends on how much is imported), so it can't be flattened into a fixed
    price array; use TariffEnergyCharge (premise.tariff) instead, which embeds the
    tiers directly as optimization constraints."""

    def __init__(self, tariff: TariffSchedule, stream: str = "import"):
        self.tariff = tariff
        self.stream = stream

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        return self.tariff.energy_rate_array(time_grid, stream=self.stream)
