"""A utility retail tariff itemized the way the utility bills it: per-kWh components,
each tagged with the bucket that determines what export credits may offset it, plus a
daily fixed charge. Loaded from a YAML file transcribed from the utility's tariff sheet
(see examples/data/tariffs/), and checked on load against the sheet's published totals.

Scope: time-of-use periods that are the same every day (true of PG&E's E-ELEC); tiered
or baseline-credit rates and weekday/weekend-specific periods aren't supported yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from der_opt.pricing.sources import PriceSeries
from der_opt.time_grid import TimeGrid

BUCKETS = ("generation", "delivery", "nbc")


@dataclass(frozen=True)
class TariffComponent:
    name: str
    bucket: str
    rates: dict[tuple[str, str], float]  # (season, period) -> $/kWh


@dataclass(frozen=True)
class RetailTariff:
    name: str
    effective_date: str
    season_by_month: dict[int, str]
    period_by_hour: dict[int, str]
    components: tuple[TariffComponent, ...]
    base_services_charge_per_day: dict[str, float]

    def bucket_rates(self, timestamps) -> pd.DataFrame:
        """$/kWh per bucket (generation, delivery, nbc) for each interval timestamp."""
        index = pd.DatetimeIndex(timestamps)
        seasons = [self.season_by_month[m] for m in index.month]
        periods = [self.period_by_hour[h] for h in index.hour]
        keys = list(zip(seasons, periods))
        out = {}
        for bucket in BUCKETS:
            per_key = {
                key: sum(c.rates[key] for c in self.components if c.bucket == bucket) for key in set(keys)
            }
            out[bucket] = [per_key[key] for key in keys]
        return pd.DataFrame(out, index=index)

    def total_rate(self, timestamps) -> np.ndarray:
        return self.bucket_rates(timestamps).sum(axis=1).to_numpy()


def _expand_rate(name: str, rate, seasons: set[str], periods: set[str]) -> dict[tuple[str, str], float]:
    if isinstance(rate, (int, float)):
        return {(s, p): float(rate) for s in seasons for p in periods}
    expanded = {}
    for season in seasons:
        if season not in rate:
            raise ValueError(f"component {name!r} has no rates for season {season!r}")
        for period in periods:
            if period not in rate[season]:
                raise ValueError(f"component {name!r} has no {season} rate for period {period!r}")
            expanded[(season, period)] = float(rate[season][period])
    return expanded


def load_retail_tariff(path: str | Path) -> RetailTariff:
    data = yaml.safe_load(Path(path).read_text())

    season_by_month: dict[int, str] = {}
    for season, months in data["seasons"].items():
        for month in months:
            if month in season_by_month:
                raise ValueError(f"month {month} assigned to more than one season")
            season_by_month[month] = season
    if sorted(season_by_month) != list(range(1, 13)):
        raise ValueError(f"seasons must cover months 1-12 exactly once; got {sorted(season_by_month)}")

    period_by_hour: dict[int, str] = {}
    for period, hours in data["periods_by_hour"].items():
        for hour in hours:
            if hour in period_by_hour:
                raise ValueError(f"hour {hour} assigned to more than one period")
            period_by_hour[hour] = period
    if sorted(period_by_hour) != list(range(24)):
        raise ValueError(f"periods_by_hour must cover hours 0-23 exactly once; got {sorted(period_by_hour)}")

    seasons, periods = set(season_by_month.values()), set(period_by_hour.values())
    components = []
    for name, spec in data["components"].items():
        if spec["bucket"] not in BUCKETS:
            raise ValueError(f"component {name!r} has bucket {spec['bucket']!r}; expected one of {BUCKETS}")
        components.append(TariffComponent(name, spec["bucket"], _expand_rate(name, spec["rate"], seasons, periods)))

    published = data.get("published_totals")
    if published:
        mismatches = []
        for season in seasons:
            for period in periods:
                summed = sum(c.rates[(season, period)] for c in components)
                expected = float(published[season][period])
                if abs(summed - expected) > 1e-5:
                    mismatches.append(f"{season}/{period}: components sum to {summed:.5f}, published {expected:.5f}")
        if mismatches:
            raise ValueError(
                f"{path}: components don't add up to the published totals -- a rate was "
                "probably mistyped or one sheet is out of date:\n  " + "\n  ".join(mismatches)
            )

    return RetailTariff(
        name=data["name"],
        effective_date=str(data.get("effective_date", "")),
        season_by_month=season_by_month,
        period_by_hour=period_by_hour,
        components=tuple(components),
        base_services_charge_per_day=dict(data.get("base_services_charge_per_day", {})),
    )


class RetailTariffPriceSeries(PriceSeries):
    """The total $/kWh import price from a RetailTariff (all components, all buckets),
    so a scenario can optimize against the same rates it's billed on."""

    def __init__(self, tariff: RetailTariff):
        self.tariff = tariff

    def values(self, time_grid: TimeGrid) -> np.ndarray:
        if time_grid.start is None:
            raise ValueError("TimeGrid.start is required for RetailTariffPriceSeries")
        timestamps: list[datetime] = time_grid.timestamps()
        return self.tariff.total_rate(timestamps)
