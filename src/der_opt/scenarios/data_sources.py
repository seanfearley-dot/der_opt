"""Named data-source resolvers for scenario YAML: any array-valued device/stream
parameter can be a literal list OR a `{source: "<name>", ...kwargs}` dict, resolved
here against this toolkit's real integrations (LBNL load data, the EV charging model,
PG&E NBT export rates, PVWatts) at scenario-load time. This is what lets a YAML
scenario reach real data without writing Python -- see examples/scenarios/ for
reproductions of the Python-API examples using this.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from der_opt.time_grid import TimeGrid

_RESOLVERS: dict[str, Any] = {}


def _register(name):
    def decorator(fn):
        _RESOLVERS[name] = fn
        return fn

    return decorator


@_register("lbnl_net_load")
def _resolve_lbnl_net_load(time_grid: TimeGrid, **kwargs) -> np.ndarray:
    from der_opt.integrations.lbnl_load import premise_net_load_kw

    return premise_net_load_kw(
        kwargs["scenario_dir"], kwargs["cluster_name"], time_grid,
        start_hour_ending=kwargs.get("start_hour_ending", 1),
    )


@_register("lbnl_gross_load")
def _resolve_lbnl_gross_load(time_grid: TimeGrid, **kwargs) -> np.ndarray:
    from der_opt.integrations.lbnl_load import premise_gross_load_kw

    return premise_gross_load_kw(
        kwargs["scenario_dir"], kwargs["cluster_name"], time_grid,
        start_hour_ending=kwargs.get("start_hour_ending", 1),
    )


@_register("lbnl_pv")
def _resolve_lbnl_pv(time_grid: TimeGrid, **kwargs) -> np.ndarray:
    from der_opt.integrations.lbnl_load import premise_solar_available_kw

    return premise_solar_available_kw(
        kwargs["scenario_dir"], kwargs["cluster_name"], time_grid,
        start_hour_ending=kwargs.get("start_hour_ending", 1),
    )


@_register("pvwatts")
def _resolve_pvwatts(time_grid: TimeGrid, **kwargs) -> np.ndarray:
    from der_opt.integrations.pvwatts import (
        fetch_pvwatts_hourly_ac_kw,
        solar_availability_kw,
    )

    hourly = fetch_pvwatts_hourly_ac_kw(
        lat=kwargs["lat"], lon=kwargs["lon"], system_capacity_kw=kwargs["system_capacity_kw"],
        azimuth=kwargs.get("azimuth", 180.0), tilt=kwargs.get("tilt"),
        array_type=kwargs.get("array_type", 1), module_type=kwargs.get("module_type", 0),
        losses=kwargs.get("losses", 14.0), api_key=kwargs.get("api_key"),
    )
    return solar_availability_kw(time_grid, hourly, start_hour=kwargs.get("start_hour", 0))


@_register("ev_connected_schedule")
def _resolve_ev_connected_schedule(time_grid: TimeGrid, **kwargs) -> np.ndarray:
    from der_opt.integrations.ev_profile import (
        connected_schedule,
        typical_arrival_departure_hours,
    )

    arrival_hour, departure_hour = typical_arrival_departure_hours(
        kwargs["arrivals_csv"], kwargs["departures_csv"], kwargs.get("percentile", 50.0)
    )
    return connected_schedule(time_grid, arrival_hour, departure_hour)


def resolve_array_param(value: Any, time_grid: TimeGrid) -> Any:
    """If `value` is a `{source: "<name>", ...kwargs}` dict, resolves it against the
    named data source and returns a numeric array; otherwise returns `value`
    unchanged (a literal list, scalar, or already-resolved array)."""
    if not (isinstance(value, dict) and "source" in value):
        return value
    source = value["source"]
    if source not in _RESOLVERS:
        raise ValueError(f"Unknown data source {source!r}; choices: {sorted(_RESOLVERS)}")
    kwargs = {k: v for k, v in value.items() if k != "source"}
    return _RESOLVERS[source](time_grid, **kwargs)
