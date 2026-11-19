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


class CSVPriceSeries(PriceSeries):
    """Wraps a user-provided series: a plain array already aligned to the TimeGrid, or a
    pandas Series indexed by timestamp (resampled/forward-filled onto the TimeGrid)."""

    def __init__(self, series):
        self._series = series

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if isinstance(self._series, pd.Series):
            if time_grid.start is None:
                raise ValueError(
                    "TimeGrid.start is required to align a timestamp-indexed CSVPriceSeries"
                )
            timestamps = pd.DatetimeIndex(time_grid.timestamps())
            resampled = self._series.reindex(timestamps, method="ffill")
            if resampled.isna().any():
                raise ValueError("CSVPriceSeries does not cover the full TimeGrid horizon")
            return resampled.to_numpy(dtype=float)

        arr = np.asarray(self._series, dtype=float)
        if arr.shape[0] != time_grid.horizon_steps:
            raise ValueError(
                f"CSVPriceSeries array length {arr.shape[0]} does not match "
                f"time_grid.horizon_steps {time_grid.horizon_steps}"
            )
        return arr


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
