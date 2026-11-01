"""Adapter for the LBNL-LOAD Phase 4 dataset (California Demand Response Potential
Study, Phase 4 -- Gerke et al. 2024, LBNL-2001596) as a premise load-profile source.

Dataset shape (see the dataset's own METHODOLOGY.md): each scenario is a folder
containing `cluster_summary.csv` (one row per customer cluster, with a `customer_count`
energy-weighted extrapolation factor) and one `<cluster_name>.csv` per cluster -- 8760
hourly rows (`hour_ending` 1-8760) with per-end-use kWh columns plus `total` (gross
consumption, NOT net of PV) and `pv_generation` (always positive). Both are
cluster-level (summed across all represented customers), not per-customer, so getting a
single premise's profile means dividing by that cluster's `customer_count`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.time_grid import TimeGrid


def _read_cluster_summary(scenario_dir: Path) -> pd.DataFrame:
    path = scenario_dir / "cluster_summary.csv"
    if not path.exists():
        raise FileNotFoundError(f"No cluster_summary.csv in {scenario_dir}")
    return pd.read_csv(path)


def list_clusters(scenario_dir: str | Path, sector: str | None = None) -> list[str]:
    """Cluster names available in a scenario folder (the `name` column of its
    cluster_summary.csv), optionally filtered to one `sector` (e.g. "res")."""
    summary = _read_cluster_summary(Path(scenario_dir))
    if sector is not None:
        summary = summary[summary["sector"] == sector]
    return summary["name"].tolist()


def _customer_count(scenario_dir: Path, cluster_name: str) -> float:
    summary = _read_cluster_summary(scenario_dir)
    row = summary.loc[summary["name"] == cluster_name]
    if row.empty:
        raise ValueError(f"Cluster {cluster_name!r} not found in {scenario_dir / 'cluster_summary.csv'}")
    return float(row["customer_count"].iloc[0])


def load_cluster_hourly(scenario_dir: str | Path, cluster_name: str) -> pd.DataFrame:
    """Raw per-cluster hourly data (hour_ending, end-use columns, total, pv_generation)
    -- cluster-level (energy-weighted-extrapolated) kWh/hour, not per-customer."""
    scenario_dir = Path(scenario_dir)
    path = scenario_dir / f"{cluster_name}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _slice_hours(series: pd.Series, time_grid: TimeGrid, start_hour_ending: int) -> np.ndarray:
    if time_grid.resolution_minutes != 60:
        raise ValueError(
            "LBNL Phase 4 cluster data is hourly; use a TimeGrid with resolution_minutes=60"
        )
    if start_hour_ending < 1:
        raise ValueError("start_hour_ending is 1-indexed (hour_ending convention); must be >= 1")
    end = start_hour_ending + time_grid.horizon_steps - 1
    if end > len(series):
        raise ValueError(
            f"Requested hour_ending range [{start_hour_ending}, {end}] exceeds the "
            f"{len(series)}-hour cluster profile"
        )
    return series.iloc[start_hour_ending - 1 : end].to_numpy()


def premise_net_load_kw(
    scenario_dir: str | Path, cluster_name: str, time_grid: TimeGrid, start_hour_ending: int = 1
) -> np.ndarray:
    """Per-customer net load (kW) for one cluster -- (total - pv_generation) /
    customer_count -- sliced to time_grid's horizon. hour_ending=1 is the first hour of
    the scenario's modeled year; pass start_hour_ending to pull a sub-year window (the
    data has no calendar-date column, only hour_ending, so aligning to a specific
    calendar date is the caller's responsibility). Treating hourly kWh as kW is the
    standard hourly-load-data simplification."""
    scenario_dir = Path(scenario_dir)
    customer_count = _customer_count(scenario_dir, cluster_name)
    hourly = load_cluster_hourly(scenario_dir, cluster_name)
    net_kwh_per_customer = (hourly["total"] - hourly["pv_generation"]) / customer_count
    return _slice_hours(net_kwh_per_customer, time_grid, start_hour_ending)


def premise_gross_load_kw(
    scenario_dir: str | Path, cluster_name: str, time_grid: TimeGrid, start_hour_ending: int = 1
) -> np.ndarray:
    """Per-customer gross load (kW) -- `total` / customer_count, PV NOT subtracted --
    sliced the same way as premise_net_load_kw. Use this (not premise_net_load_kw) as a
    Premise's external_load when solar is modeled as its own SolarPV device (e.g. via
    premise_solar_available_kw below): subtracting PV from external_load AND also
    dispatching a SolarPV device on the same premise would double-count it."""
    scenario_dir = Path(scenario_dir)
    customer_count = _customer_count(scenario_dir, cluster_name)
    hourly = load_cluster_hourly(scenario_dir, cluster_name)
    gross_kwh_per_customer = hourly["total"] / customer_count
    return _slice_hours(gross_kwh_per_customer, time_grid, start_hour_ending)


def premise_solar_available_kw(
    scenario_dir: str | Path, cluster_name: str, time_grid: TimeGrid, start_hour_ending: int = 1
) -> np.ndarray:
    """Per-customer behind-the-meter PV generation (kW) embedded in the cluster -- a
    SolarPV availability-profile proxy for this cluster's segment, sliced the same way
    as premise_net_load_kw. Clusters with pv_penetration near 0 will return ~0 here."""
    scenario_dir = Path(scenario_dir)
    customer_count = _customer_count(scenario_dir, cluster_name)
    hourly = load_cluster_hourly(scenario_dir, cluster_name)
    pv_kwh_per_customer = hourly["pv_generation"] / customer_count
    return _slice_hours(pv_kwh_per_customer, time_grid, start_hour_ending)
