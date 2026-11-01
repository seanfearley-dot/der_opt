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
    or any other price source plug into EnergyArbitrage (or anything else) interchangeably."""

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


class LMPPriceSeries(PriceSeries):
    """Wraps a wholesale LMP pull (via the optional `gridstatus` dependency) into a
    per-period array. ISO LMP methods are source-specific, so each ISO needs its own
    adapter here; only CAISO is implemented so far."""

    def __init__(self, iso: str, market: str, node: str | None = None):
        self.iso = iso.upper()
        self.market = market
        self.node = node

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if time_grid.start is None:
            raise ValueError("TimeGrid.start is required to fetch LMP data")
        if self.iso != "CAISO":
            raise NotImplementedError(f"LMPPriceSeries does not yet support ISO {self.iso!r}")
        series = self._fetch_caiso_lmp(time_grid)
        return CSVPriceSeries(series).values(time_grid)

    def _fetch_caiso_lmp(self, time_grid: TimeGrid) -> pd.Series:
        try:
            import gridstatus
        except ImportError as exc:
            raise ImportError(
                "LMPPriceSeries requires the optional 'gridstatus' dependency: "
                "pip install 'der-opt[lmp]'"
            ) from exc

        caiso = gridstatus.CAISO()
        start = time_grid.start
        end = start + pd.Timedelta(hours=time_grid.horizon_steps * time_grid.dt_hours)
        locations = [self.node] if self.node else "ALL"
        df = caiso.get_lmp(start=start, end=end, market=self.market, locations=locations)
        return (df.set_index("Time")["LMP"] / 1000.0).sort_index()  # $/MWh -> $/kWh
