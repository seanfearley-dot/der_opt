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
from der_opt.pricing.sources import (
    ConstantPriceSeries,
    CSVPriceSeries,
    DailyTOUPriceSeries,
    PriceSeries,
)
from der_opt.scenarios.data_sources import resolve_array_param
from der_opt.scenarios.schema import (
    CSVSeriesConfig,
    DeviceConfig,
    ScenarioConfig,
    ValueStreamConfig,
)
from der_opt.simulation import (
    ControlPolicy,
    NoFlexibilityPolicy,
    RuleBasedEVChargingPolicy,
    SelfConsumptionBatteryPolicy,
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


def _build_device(config: DeviceConfig, time_grid: TimeGrid, built_devices: dict[str, Device]) -> Device:
    """Resolves any `{source: ...}` data-source params (e.g. available_kw from the real
    LBNL PV profile, connected from the real EV arrival/departure data) and
    `charge_sources` (device *names*, looked up against devices already built earlier
    in this scenario's device list -- so a battery/EV referencing a solar device must
    be listed after it in the YAML)."""
    cls = DEVICE_REGISTRY[config.type]
    params = {}
    for key, value in config.params.items():
        if key == "charge_sources":
            missing = [name for name in value if name not in built_devices]
            if missing:
                raise ValueError(
                    f"charge_sources {missing} not found -- they must be listed earlier "
                    f"in this scenario's devices than {config.name!r}"
                )
            params[key] = [built_devices[name] for name in value]
        else:
            params[key] = resolve_array_param(value, time_grid)
    return cls(name=config.name, time_grid=time_grid, **params)


def _read_csv_series(config: CSVSeriesConfig) -> pd.Series:
    frame = pd.read_csv(config.path, parse_dates=[0], index_col=0)
    return frame[config.column]


def _build_price_series(config: dict) -> PriceSeries:
    kind = config["type"]
    if kind == "constant":
        return ConstantPriceSeries(config["price"])
    if kind == "csv":
        return CSVPriceSeries(_read_csv_series(CSVSeriesConfig(**config)))
    if kind == "daily_tou":
        return DailyTOUPriceSeries(config["hourly_pattern"])
    if kind == "nbt_export":
        from der_opt.integrations.pge_nbt_export import NBTExportPriceSeries

        return NBTExportPriceSeries(
            config["csv_path"],
            include_produced=config.get("include_produced", True),
            adder_per_kwh=config.get("adder_per_kwh", 0.0),
        )
    if kind == "retail_tariff":
        from der_opt.billing import RetailTariffPriceSeries, load_retail_tariff

        return RetailTariffPriceSeries(load_retail_tariff(config["path"]))
    raise ValueError(
        f"Unknown price series type {kind!r}; expected 'constant', 'csv', 'daily_tou', "
        "'nbt_export', or 'retail_tariff'"
    )


def _build_tariff(config: dict) -> TariffSchedule:
    if "urdb_json_path" in config:
        return TariffSchedule.from_urdb_json(config["urdb_json_path"])
    raise ValueError(
        "tariff config must include urdb_json_path (building a tariff from inline "
        "rate tables in YAML is a documented future extension, not yet supported)"
    )


def _build_value_stream(config: ValueStreamConfig, time_grid: TimeGrid, scenario: ScenarioConfig) -> ValueStream:
    params = config.params
    if config.type in ("nbt_bill", "nem2_bill"):
        billing = build_billing(scenario)
        if billing is None:
            raise ValueError(
                f"value stream {config.type} optimizes the bill, so the scenario needs a billing section"
            )
        if any(stream.type == "energy_arbitrage" for stream in scenario.value_streams):
            raise ValueError(
                f"{config.type} and energy_arbitrage both price grid import/export -- use one, "
                "or energy is counted twice"
            )
        from der_opt.billing import NBTProgram, NEM2Program
        from der_opt.billing.objective import NBTBill, NEM2Bill

        tariff, program, kwargs = billing
        if config.type == "nem2_bill":
            if not isinstance(program, NEM2Program):
                raise ValueError("value stream nem2_bill needs billing.program type: nem2")
            return NEM2Bill(tariff, program, time_grid, **kwargs)
        if program is not None and not isinstance(program, NBTProgram):
            raise ValueError("value stream nbt_bill needs billing.program type: nbt")
        return NBTBill(tariff, program, time_grid, **kwargs)
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
    if config is None:
        return np.zeros(time_grid.horizon_steps)
    if isinstance(config, list):
        return np.asarray(config, dtype=float)
    if "source" in config:
        return np.asarray(resolve_array_param(config, time_grid), dtype=float)
    if config.get("type") == "csv":
        # Reuses CSVPriceSeries purely for its timestamp-resample/align logic, not
        # because a load profile is a price -- same mechanics (a timestamp-indexed
        # column aligned onto a TimeGrid), no need for a second implementation.
        series = _read_csv_series(CSVSeriesConfig(**config))
        return CSVPriceSeries(series).values(time_grid)
    raise ValueError(
        f"external_load dict must have 'source' (a data-source resolver) or "
        f"type: csv; got keys {list(config.keys())}"
    )


def load_scenario_config(path: str | Path) -> ScenarioConfig:
    """Parses and validates a scenario YAML file without building anything."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ScenarioConfig.model_validate(raw)


def build_premise(config: ScenarioConfig) -> Premise:
    time_grid = _build_time_grid(config.time_grid)

    built_devices: dict[str, Device] = {}
    for device_config in config.devices:
        built_devices[device_config.name] = _build_device(device_config, time_grid, built_devices)
    devices = list(built_devices.values())

    value_streams = [_build_value_stream(stream_config, time_grid, config) for stream_config in config.value_streams]
    external_load = _resolve_external_load(config.external_load, time_grid)

    return Premise(
        name=config.name,
        time_grid=time_grid,
        devices=devices,
        value_streams=value_streams,
        external_load=external_load,
        poi_limit_kw=config.poi_limit_kw,
        exclusive_import_export=config.exclusive_import_export,
    )


def load_scenario(path: str | Path) -> Premise:
    """Builds a Premise directly from a scenario YAML file -- the config-driven layer
    on top of the Python API (same Premise/Device/ValueStream objects, scenarios/
    loader.py just assembles them from YAML instead of a script). See
    examples/scenarios/ for scenarios that pull real LBNL load/PV, real EV plug-in
    schedules, and real PG&E NBT export rates entirely from YAML."""
    return build_premise(load_scenario_config(path))


def _build_policy(device_name: str, policy_config: dict, device: Device) -> ControlPolicy:
    kind = policy_config.get("type")
    overrides = {k: v for k, v in policy_config.items() if k != "type"}

    def device_attr(attr: str):
        if attr in overrides:
            return overrides[attr]
        if not hasattr(device, attr):
            raise ValueError(
                f"counterfactual policy {kind!r} on {device_name!r} needs `{attr}`, which a "
                f"{type(device).__name__} doesn't have -- wrong policy for this device type?"
            )
        return getattr(device, attr)

    if kind == "no_flexibility":
        return NoFlexibilityPolicy()
    if kind == "self_consumption":
        return SelfConsumptionBatteryPolicy(
            p_max_charge_kw=device_attr("p_max_charge_kw"),
            p_max_discharge_kw=device_attr("p_max_discharge_kw"),
        )
    if kind == "rule_based_ev":
        return RuleBasedEVChargingPolicy(
            p_max_charge_kw=device_attr("p_max_charge_kw"), soc_max=device_attr("soc_max")
        )
    raise ValueError(
        f"Unknown counterfactual policy type {kind!r} for {device_name!r}; "
        "expected 'self_consumption', 'rule_based_ev', or 'no_flexibility'"
    )


def build_counterfactual_policies(config: ScenarioConfig, premise: Premise) -> dict[str, ControlPolicy] | None:
    """The scenario's counterfactual policies keyed by device name, or None if the
    scenario has no counterfactual section. Policy power limits default to the
    device's own (p_max_charge_kw etc.) unless overridden in the YAML."""
    if config.counterfactual is None:
        return None
    devices = {device.name: device for device in premise.devices}
    policies: dict[str, ControlPolicy] = {}
    for device_name, policy_config in config.counterfactual.policies.items():
        if device_name not in devices:
            raise ValueError(
                f"counterfactual policy for unknown device {device_name!r}; "
                f"devices are {sorted(devices)}"
            )
        policies[device_name] = _build_policy(device_name, policy_config, devices[device_name])
    return policies


def build_billing(config: ScenarioConfig):
    """(tariff, program, compute_bill keyword args) for the scenario's billing section,
    or None if it has none."""
    if config.billing is None:
        return None
    from der_opt.billing import NBTProgram, NEM2Program, load_retail_tariff

    billing = config.billing
    tariff = load_retail_tariff(billing.tariff)
    program = None
    if billing.program is not None:
        params = dict(billing.program)
        kind = params.pop("type", None)
        if kind == "nbt":
            program = NBTProgram(**params)
        elif kind == "nem2":
            program = NEM2Program(**params)
        else:
            raise ValueError(f"Unknown billing program type {kind!r}; expected 'nbt' or 'nem2'")
    kwargs = {
        "income_tier": billing.income_tier,
        "relevant_period_cycles": billing.relevant_period_cycles,
        "baseline_territory": billing.baseline_territory,
        "baseline_code": billing.baseline_code,
    }
    if billing.cycle_start_dates:
        kwargs["cycle_start_dates"] = [datetime.fromisoformat(d).date() for d in billing.cycle_start_dates]
    return tariff, program, kwargs
