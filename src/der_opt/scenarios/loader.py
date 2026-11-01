from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from der_opt.devices import (
    BatteryStorage,
    CurtailableLoad,
    SmartThermostat,
    SolarPV,
    V2XVehicle,
)
from der_opt.devices.base import Device
from der_opt.premise import (
    CapacityReliabilityPayment,
    DemandCharge,
    EnergyArbitrage,
    Premise,
    TariffEnergyCharge,
    TariffSchedule,
    ValueStream,
)
from der_opt.pricing.sources import ConstantPriceSeries, CSVPriceSeries, PriceSeries
from der_opt.scenarios.schema import (
    CSVSeriesConfig,
    DeviceConfig,
    ScenarioConfig,
    ValueStreamConfig,
)
from der_opt.time_grid import TimeGrid

DEVICE_REGISTRY: dict[str, type[Device]] = {
    "solar": SolarPV,
    "battery": BatteryStorage,
    "thermostat": SmartThermostat,
    "curtailable_load": CurtailableLoad,
    "ev_v2x": V2XVehicle,
}


def _build_time_grid(config) -> TimeGrid:
    start = datetime.fromisoformat(config.start) if config.start else None
    return TimeGrid(resolution_minutes=config.resolution_minutes, horizon_steps=config.horizon_steps, start=start)


def _build_device(config: DeviceConfig, time_grid: TimeGrid) -> Device:
    cls = DEVICE_REGISTRY[config.type]
    return cls(name=config.name, time_grid=time_grid, **config.params)


def _read_csv_series(config: CSVSeriesConfig) -> pd.Series:
    frame = pd.read_csv(config.path, parse_dates=[0], index_col=0)
    return frame[config.column]


def _build_price_series(config: dict) -> PriceSeries:
    kind = config["type"]
    if kind == "constant":
        return ConstantPriceSeries(config["price"])
    if kind == "csv":
        return CSVPriceSeries(_read_csv_series(CSVSeriesConfig(**config)))
    raise ValueError(f"Unknown price series type {kind!r}; expected 'constant' or 'csv'")


def _build_tariff(config: dict) -> TariffSchedule:
    if "urdb_json_path" in config:
        return TariffSchedule.from_urdb_json(config["urdb_json_path"])
    raise ValueError(
        "tariff config must include urdb_json_path (building a tariff from inline "
        "rate tables in YAML is a documented future extension, not yet supported)"
    )


def _build_value_stream(config: ValueStreamConfig, time_grid: TimeGrid) -> ValueStream:
    params = config.params
    if config.type == "energy_arbitrage":
        import_price = _build_price_series(params["import_price"])
        export_price = _build_price_series(params["export_price"])
        return EnergyArbitrage(import_price, export_price, time_grid)
    if config.type == "tariff_energy_charge":
        tariff = _build_tariff(params["tariff"])
        return TariffEnergyCharge(tariff, time_grid, direction=params.get("direction", "import"))
    if config.type == "demand_charge":
        return DemandCharge(rate_per_kw=params["rate_per_kw"])
    if config.type == "capacity_payment":
        return CapacityReliabilityPayment(
            event_periods=params["event_periods"],
            payment_per_kw_event=params["payment_per_kw_event"],
            committed_kw=params["committed_kw"],
        )
    raise ValueError(f"Unknown value stream type {config.type!r}")


def _resolve_external_load(config, time_grid: TimeGrid) -> np.ndarray:
    if isinstance(config, list):
        return np.asarray(config, dtype=float)
    # Reuses CSVPriceSeries purely for its timestamp-resample/align logic, not because
    # a load profile is a price -- same mechanics (a timestamp-indexed column aligned
    # onto a TimeGrid), no need for a second implementation.
    series = _read_csv_series(config)
    return CSVPriceSeries(series).values(time_grid)


def load_scenario(path: str | Path) -> Premise:
    """Builds a Premise directly from a scenario YAML file -- the config-driven layer
    on top of the Python API (same Premise/Device/ValueStream objects, scenarios/
    loader.py just assembles them from YAML instead of a script)."""
    raw = yaml.safe_load(Path(path).read_text())
    config = ScenarioConfig.model_validate(raw)

    time_grid = _build_time_grid(config.time_grid)
    devices = [_build_device(device_config, time_grid) for device_config in config.devices]
    value_streams = [_build_value_stream(stream_config, time_grid) for stream_config in config.value_streams]
    external_load = _resolve_external_load(config.external_load, time_grid)

    return Premise(
        name=config.name,
        time_grid=time_grid,
        devices=devices,
        value_streams=value_streams,
        external_load=external_load,
        poi_limit_kw=config.poi_limit_kw,
    )
