"""Adapter for loadcast forecast files (the user's load-forecasting package) as a load-profile
source: bank, class or new-load profiles from a planning run (`loadcast simulate-year`), as one
simulated weather year or an hourly quantile.

The coupling is one-way and file-based: loadcast writes NetCDF files following its output
contract (contract_version "1"), and nothing here imports loadcast. A file holds

    gross / pv / net        (trace, time, entity) kW -- one row per simulated weather year
    gross_q / pv_q / net_q  (quantile, time, entity) kW -- hourly quantiles across traces

with ``time`` in UTC, hour-beginning, stored tz-naive, and ``entity_type`` in {bank, class,
customer}. A planning run writes ``bank_traces.nc`` (the bank: full traces + quantiles) and
``class_quantiles.nc`` (each class and new load: totals, with an ``n_customers`` coordinate).
Planning files carry the site time zone in ``attrs["tz"]``.

Choosing a profile (exactly one of ``trace`` / ``quantile``):

- ``trace``: an integer trace, or a named one by annual peak: ``"poe10"`` (the year whose peak
  is exceeded in 10% of simulated years, a 1-in-10 design year), ``"poe50"`` (median-peak year),
  ``"poe90"``, ``"max"`` (highest-peak year). A trace is a physically consistent year: one
  weather sequence, so heat, sun and load move together.
- ``quantile``: the hourly quantile across traces (e.g. 0.9). Not a year that can happen: every
  hour sits at its own 90th percentile, so it overstates sustained stress -- fine as an envelope,
  not for storage or energy studies.

``var``: "net" (what the grid sees), "gross" (consumption before behind-the-meter PV; use it when
PV is modeled as its own SolarPV device, as with lbnl_gross_load), or "pv".
``per_customer``: divide a class total by its ``n_customers`` (average customer of the class).

der_opt TimeGrids are local wall-clock time; timestamps are converted from ``tz`` (default the
file's ``attrs["tz"]``) to UTC to look values up. Each TimeGrid step takes the hour it falls in
(hourly kW held across sub-hourly steps). ``align_year=True`` maps the grid's dates onto the
forecast's year (e.g. a 2026 tariff study run on a 2025 forecast); Feb 29 maps to Feb 28.

Reading NetCDF needs the ``forecast`` extra: ``pip install -e ".[forecast]"`` (xarray, netCDF4).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from der_opt.time_grid import TimeGrid

CONTRACT_VERSION = "1"
VARIABLES = ("net", "gross", "pv")
NAMED_TRACES = {"poe10": 0.9, "poe50": 0.5, "poe90": 0.1}


def _xarray():
    try:
        import xarray as xr
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Reading loadcast forecasts needs xarray and netCDF4: pip install -e \".[forecast]\""
        ) from exc
    return xr


def open_forecast(path):
    """The forecast file as an xarray Dataset, checked against loadcast's output contract. An
    already-open Dataset is returned as is."""
    if hasattr(path, "data_vars"):
        return path
    xr = _xarray()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    ds = xr.open_dataset(path, engine="netcdf4").load()
    version = ds.attrs.get("contract_version")
    if version != CONTRACT_VERSION:
        raise ValueError(f"{path}: contract_version {version!r}, this adapter reads {CONTRACT_VERSION!r}")
    if "entity" not in ds.dims or "time" not in ds.dims:
        raise ValueError(f"{path}: not a loadcast forecast (needs time and entity dimensions)")
    ds = ds.assign_coords(entity=np.asarray(ds["entity"].values).astype(str))
    return ds


def describe_forecast(path: str | Path) -> dict:
    """What a file holds: kind, scenario, time range (UTC), entities with their types and
    customer counts, number of traces, quantile levels, and each trace's annual peak (kW) and
    when it happens (local time) for the first entity -- enough to pick a trace and a window."""
    ds = open_forecast(path)
    t = pd.DatetimeIndex(ds["time"].values)
    out = {
        "kind": ds.attrs.get("kind"), "scenario": ds.attrs.get("scenario"), "tz": ds.attrs.get("tz"),
        "time_utc": [str(t[0]), str(t[-1])], "n_hours": len(t),
        "entities": {
            str(e): {
                "type": str(ds["entity_type"].values[i]),
                **({"n_customers": float(ds["n_customers"].values[i])} if "n_customers" in ds.coords else {}),
            }
            for i, e in enumerate(ds["entity"].values)
        },
        "variables": [str(v) for v in ds.data_vars],
        "n_traces": int(ds.sizes.get("trace", 0)),
        "quantiles": [float(q) for q in ds["quantile"].values] if "quantile" in ds.coords else [],
    }
    if "net" in ds.data_vars:
        net = ds["net"].isel(entity=0).values
        tz = ZoneInfo(ds.attrs.get("tz", "UTC"))
        i_peak = net.argmax(axis=1)
        local = t.tz_localize("UTC").tz_convert(tz)
        out["trace_peaks"] = [
            {"trace": k, "peak_kw": float(net[k, i]), "peak_time_local": str(local[i].tz_localize(None))}
            for k, i in enumerate(i_peak)
        ]
        for name in NAMED_TRACES:
            out[f"{name}_trace"] = _named_trace(net, name)
    return out


def _named_trace(values: np.ndarray, name: str) -> int:
    """Index of the trace whose annual peak is closest to the named POE level."""
    peaks = values.max(axis=1)
    if name == "max":
        return int(peaks.argmax())
    if name not in NAMED_TRACES:
        raise ValueError(f"trace must be an integer or one of {sorted([*NAMED_TRACES, 'max'])}; got {name!r}")
    target = np.quantile(peaks, NAMED_TRACES[name])
    return int(np.abs(peaks - target).argmin())


def _utc_hours(time_grid: TimeGrid, tz: str, align_year: int | None) -> pd.DatetimeIndex:
    if time_grid.start is None:
        raise ValueError("TimeGrid.start is required to read a dated forecast")
    stamps = time_grid.timestamps()
    if align_year is not None:
        stamps = [_replace_year(ts, align_year) for ts in stamps]
    local = pd.DatetimeIndex(stamps).floor("h")
    utc = local.tz_localize(ZoneInfo(tz), ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")
    return utc.tz_localize(None)


def _replace_year(ts: datetime, year: int) -> datetime:
    try:
        return ts.replace(year=year)
    except ValueError:  # Feb 29 into a non-leap year
        return ts.replace(year=year, day=28)


def forecast_profile_kw(
    path,
    time_grid: TimeGrid,
    *,
    entity: str | None = None,
    var: str = "net",
    trace: int | str | None = None,
    quantile: float | None = None,
    per_customer: bool = False,
    scale: float = 1.0,
    tz: str | None = None,
    align_year: bool = False,
) -> np.ndarray:
    """One load profile (kW) on ``time_grid`` from a loadcast forecast file or an open Dataset
    (see module doc).

    ``entity`` may be omitted when the file holds a single entity. ``scale`` multiplies the
    result (e.g. a share of a class). Raises if the grid runs outside the forecast."""
    if (trace is None) == (quantile is None):
        raise ValueError("give exactly one of trace (an index or poe10/poe50/poe90/max) or quantile")
    if var not in VARIABLES:
        raise ValueError(f"var must be one of {VARIABLES}; got {var!r}")
    ds = open_forecast(path)
    entities = list(ds["entity"].values)
    if entity is None:
        if len(entities) != 1:
            raise ValueError(f"the forecast holds several entities; pass entity= one of {entities}")
        entity = entities[0]
    if entity not in entities:
        raise ValueError(f"entity {entity!r} not in the forecast; available: {entities}")
    e = entities.index(entity)
    if trace is not None:
        if var not in ds.data_vars:
            raise ValueError(f"the forecast has no traces of {var!r} (variables: {list(ds.data_vars)}); use quantile=")
        values = ds[var].isel(entity=e).values
        k = _named_trace(ds["net"].isel(entity=e).values if "net" in ds.data_vars else values, trace) \
            if isinstance(trace, str) else int(trace)
        if not 0 <= k < values.shape[0]:
            raise ValueError(f"trace {k} out of range (file has {values.shape[0]})")
        series = values[k]
    else:
        qvar = f"{var}_q"
        if qvar not in ds.data_vars:
            raise ValueError(f"the forecast has no {qvar}; available: {list(ds.data_vars)}")
        levels = np.asarray(ds["quantile"].values, dtype=float)
        j = int(np.abs(levels - quantile).argmin())
        if abs(levels[j] - quantile) > 1e-6:
            raise ValueError(f"quantile {quantile} not in the file; levels: {levels.tolist()}")
        series = ds[qvar].isel(entity=e, quantile=j).values
    if per_customer:
        if "n_customers" not in ds.coords:
            raise ValueError("the forecast has no n_customers coordinate (only class files from planning runs do)")
        n = float(ds["n_customers"].values[e])
        if not np.isfinite(n) or n <= 0:
            raise ValueError(f"entity {entity!r} has no customer count ({n})")
        series = series / n
    time = pd.DatetimeIndex(ds["time"].values)
    zone = tz or ds.attrs.get("tz")
    if zone is None:
        raise ValueError("the forecast has no tz attribute; pass tz= (e.g. America/Los_Angeles)")
    year = pd.Timestamp(time[len(time) // 2]).tz_localize("UTC").tz_convert(ZoneInfo(zone)).year if align_year else None
    want = _utc_hours(time_grid, zone, year)
    pos = time.get_indexer(want)
    if (pos < 0).any():
        bad = want[pos < 0]
        raise ValueError(
            f"time grid runs outside the forecast ({time[0]} to {time[-1]} UTC): first missing hour "
            f"{bad[0]} UTC; set align_year=True to map the grid onto the forecast's year"
        )
    return np.asarray(series, dtype=float)[pos] * scale


def forecast_traces_kw(
    path, time_grid: TimeGrid, *, entity: str | None = None, var: str = "net",
    tz: str | None = None, align_year: bool = False,
) -> np.ndarray:
    """Every trace on ``time_grid``: (n_traces, horizon_steps) kW, for running a dispatch across
    weather years."""
    ds = open_forecast(path)
    n = int(ds.sizes.get("trace", 0))
    if n == 0:
        raise ValueError("the forecast has no traces")
    return np.vstack([
        forecast_profile_kw(ds, time_grid, entity=entity, var=var, trace=k, tz=tz, align_year=align_year)
        for k in range(n)
    ])
