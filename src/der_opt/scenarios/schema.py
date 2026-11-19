from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

# pydantic v2 evaluates field annotations at runtime to build validators (unlike plain
# dataclasses, which never do even with `from __future__ import annotations`), and
# Python 3.9 doesn't support the `X | None` syntax at runtime -- so these models use
# typing.Optional/Union explicitly rather than the newer `|` syntax used elsewhere in
# this codebase.


class TimeGridConfig(BaseModel):
    resolution_minutes: int
    horizon_steps: int
    start: Optional[str] = None  # ISO-8601, e.g. "2026-07-15T00:00:00"


class DeviceConfig(BaseModel):
    type: Literal["solar", "battery", "thermostat", "curtailable_load", "ev_v2x"]
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ValueStreamConfig(BaseModel):
    type: Literal["energy_arbitrage", "tariff_energy_charge", "demand_charge", "capacity_payment", "nbt_bill"]
    params: dict[str, Any] = Field(default_factory=dict)


class CSVSeriesConfig(BaseModel):
    type: Literal["csv"] = "csv"
    path: str
    column: str


class CounterfactualConfig(BaseModel):
    """Rule-based baseline to compare the optimal dispatch against. `policies` maps a
    device name to {type: ..., optional overrides}; devices not listed run their
    passive default (solar generates everything available, storage/EV/thermostat
    idle, curtailable load fully served)."""

    policies: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BillingConfig(BaseModel):
    """Bills the optimal (and counterfactual) dispatch on a retail tariff. `program` is
    {type: nbt, export_rates_csv: ..., acc_plus_per_kwh: ..., ...} (see
    der_opt.billing.NBTProgram for all fields), or omitted for no export credit.
    `cycle_start_dates` are meter-read dates (ISO); calendar months if omitted."""

    tariff: str
    income_tier: int = 3
    program: Optional[dict[str, Any]] = None
    cycle_start_dates: Optional[list[str]] = None
    relevant_period_cycles: int = 12


class ScenarioConfig(BaseModel):
    """Top-level structure of a scenario YAML file. Per-device and per-value-stream
    `params` are intentionally left as loose dicts rather than one pydantic model per
    device/stream type -- they're passed straight through as the real class's kwargs
    (see scenarios/loader.py's registries), so this validates scenario *structure*
    (every section is present and shaped right) without duplicating every device's
    __init__ signature a second time here."""

    name: str
    time_grid: TimeGridConfig
    devices: list[DeviceConfig] = Field(default_factory=list)
    value_streams: list[ValueStreamConfig] = Field(default_factory=list)
    # A literal array, {type: csv, path, column}, or {source: "<name>", ...kwargs} for
    # one of scenarios/data_sources.py's resolvers (e.g. real LBNL load data) -- left
    # as a generic dict rather than a stricter union so the loader (which already has
    # to dispatch on "type"/"source" for devices and value streams) is the single place
    # that decides what shape a dict here means, instead of relying on pydantic to pick
    # the right union member among several same-shaped dict models.
    external_load: Union[list[float], dict[str, Any]]
    poi_limit_kw: Optional[float] = None
    exclusive_import_export: bool = False
    counterfactual: Optional[CounterfactualConfig] = None
    billing: Optional[BillingConfig] = None
