from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TimeGridConfig(BaseModel):
    resolution_minutes: int
    horizon_steps: int
    start: str | None = None  # ISO-8601, e.g. "2026-07-15T00:00:00"


class DeviceConfig(BaseModel):
    type: Literal["solar", "battery", "thermostat", "curtailable_load", "ev_v2x"]
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ValueStreamConfig(BaseModel):
    # `bill` = nbt_bill or nem2_bill, whichever matches the billing program
    type: Literal["energy_arbitrage", "tariff_energy_charge", "demand_charge", "capacity_payment", "nbt_bill", "nem2_bill", "bill"]
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
    der_opt.billing.NBTProgram), {type: nem2, nsc_rate_per_kwh: ...} (see
    der_opt.billing.NEM2Program), or omitted for no export credit.
    `cycle_start_dates` are meter-read dates (ISO); calendar months if omitted.
    `baseline_territory` (e.g. "X") and `baseline_code` ("basic"/"all_electric") are
    needed for a tariff with a baseline credit, such as E-TOU-C."""

    tariff: str
    income_tier: int = 3
    program: dict[str, Any] | None = None
    cycle_start_dates: list[str] | None = None
    relevant_period_cycles: int = 12
    baseline_territory: str | None = None
    baseline_code: str = "basic"


# --- demand response (docs/DR_CAPACITY_SPEC.md v1.0) ----------------------------------


class DRProgramConfig(BaseModel):
    program_id: str
    season: list[int] = Field(default_factory=lambda: list(range(1, 13)))
    day_types: list[Literal["weekday", "weekend", "holiday"]] = Field(default_factory=lambda: ["weekday"])
    window: str = "16:00-21:00"  # local hours events may fall in
    min_hours: float = 1.0
    max_hours: float = 4.0
    max_events_per_year: int | None = None
    max_events_per_month: int | None = None
    max_consecutive_days: int | None = None
    response: Literal["incentive", "commitment", "import_cap"] = "incentive"
    incentive_per_kwh: float | None = None
    committed_kw: float | None = None
    cap_kw: float | None = None
    shortfall_penalty_per_kwh: float = 5.0  # commitment / import_cap: keeps the run feasible
    # what incentive and commitment settle on: net load (exports count) or grid import only
    settlement_basis: Literal["net", "import"] = "net"
    settlement: Literal["by_class"] = "by_class"
    capacity_reference: Literal["R2", "R3", "S", "all"] = "all"


class DRPriceEventsConfig(BaseModel):
    series: dict[str, Any]  # any price series: {type: timeseries, path: ..., column: ...}, retail_tariff, ...
    events_per_year: int | None = None
    events_per_month: int | None = None
    duration: Literal["fixed", "variable"] = "fixed"
    hours: float | None = None  # fixed duration; default the program's max_hours
    alpha: float = 0.8  # variable: extend while price >= alpha x the day's window peak
    event_type: Literal["dispatch", "test"] = "dispatch"


class DRRandomEventsConfig(BaseModel):
    events_per_month: int | dict[int, int]
    seed: int = 0
    replicates: int = 1
    weighting: Literal["uniform", "price"] = "uniform"
    series: dict[str, Any] | None = None  # needed for weighting: price
    weight_power: float = 1.0
    min_days_between: int = 0
    hours: float | None = None  # fixed duration; omit to draw from min_hours..max_hours
    event_type: Literal["dispatch", "test"] = "dispatch"


class DRScheduleRule(BaseModel):
    start: str  # "HH:MM", local
    hours: float
    event_type: Literal["dispatch", "test"] = "dispatch"
    months: list[int] | Literal["all"] = "all"
    days: list[str] | Literal["all", "weekdays", "weekends"] = "all"
    nth_weekday: dict[str, Any] | None = None  # {n: 1, weekday: tue}
    dates: list[str] | None = None


class DREventsConfig(BaseModel):
    """Exactly one constructor."""

    file: str | None = None
    price: DRPriceEventsConfig | None = None
    random: DRRandomEventsConfig | None = None
    schedule: list[DRScheduleRule] | None = None


class DRConfig(BaseModel):
    program: DRProgramConfig
    events: DREventsConfig
    customer_class: Literal["residential", "commercial"] = "residential"
    measure_devices: list[str] | None = None  # the measure's devices (R3 removes them); default all
    tz: str = "America/Los_Angeles"
    value_series: dict[str, Any] | None = None  # places shadow events; default the price constructor's series
    shadow_events: bool = True  # counted capacity (one small solve per eligible day)


# --- avoided costs and cost tests (docs/AVOIDED_COST_SPEC.md, docs/COST_TEST_SPEC.md) --


class AvoidedCostsConfig(BaseModel):
    set: str  # an avoided cost set folder


class CapacityValueConfig(BaseModel):
    reference: Literal["R2", "R3", "S"] = "R2"
    basis: Literal["net", "import"] = "import"
    measure: Literal["counted", "accounted"] = "counted"

    @property
    def label(self) -> str:
        return f"{self.reference}/{self.basis}/{self.measure}"


class ParticipantCostsConfig(BaseModel):
    upfront: float = 0.0
    annual_om: float = 0.0
    replacements: list[dict[str, float]] = Field(default_factory=list)  # [{year: 2032, cost: 300}]


class ProgramCostsConfig(BaseModel):
    admin_upfront: float = 0.0
    admin_annual: float = 0.0
    upfront_incentive: float = 0.0


class DiscountRatesConfig(BaseModel):
    utility: float  # RIM, TRC, PAC
    participant: float  # PCT
    societal: float  # SCT


class CostTestConfig(BaseModel):
    scope: Literal["control", "device"] = "control"
    measure_devices: list[str] | None = None  # device scope: removed for R3; default dr.measure_devices or all
    start_year: int
    measure_life_years: int
    avoided_costs: AvoidedCostsConfig
    capacity_value: CapacityValueConfig = Field(default_factory=CapacityValueConfig)
    net_to_gross: float = 1.0
    impact_degradation: float = 0.0  # approved: 0 for now (OpenBCA holds a shape constant)
    bill_escalation: float = 0.0
    cost_escalation: float = 0.0
    participant_costs: ParticipantCostsConfig = Field(default_factory=ParticipantCostsConfig)
    program_costs: ProgramCostsConfig = Field(default_factory=ProgramCostsConfig)
    tax_credits: float = 0.0
    # OpenBCA's tables carry the run settings, so the export needs these (spec v1.1)
    discount_rates: DiscountRatesConfig | None = None
    tests: list[Literal["PCT", "RIM", "TRC", "PAC", "SCT"]] = Field(
        default_factory=lambda: ["PCT", "RIM", "TRC", "PAC", "SCT"])


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
    external_load: list[float] | dict[str, Any] | None = None  # omitted = no load
    poi_limit_kw: float | None = None
    import_limit_kw: float | None = None  # overrides poi_limit_kw for import
    export_limit_kw: float | None = None  # overrides poi_limit_kw for export (0 = no export)
    exclusive_import_export: bool = False
    counterfactual: CounterfactualConfig | None = None
    billing: BillingConfig | None = None
    dr: DRConfig | None = None  # demand response program and events (single-premise runs)
    weather_case: str | None = None  # the load forecast scenario's weather label, recorded only
    avoided_costs: AvoidedCostsConfig | None = None  # first-year screening values
    cost_test: CostTestConfig | None = None  # measure impact package and OpenBCA export


# --- feeder (multi-premise) scenarios ------------------------------------------------


class NetworkElementConfig(BaseModel):
    name: str
    rating_kw: float | None = None
    overload_cost_per_kwh: float | None = None  # omitted = hard limit
    parent: str | None = None


class NetworkValueStreamConfig(BaseModel):
    type: Literal["capacity_deferral"]
    element: str
    value_per_kw: float
    threshold_kw: float = 0.0


class BackgroundLoadConfig(BaseModel):
    """kW through an element in every run that isn't a customer: not billed, never
    flexible (docs/COORDINATION_SPEC.md section 1)."""

    name: str
    element: str
    load: list[float] | dict[str, Any]  # a literal array or a data source, like external_load


class FeederConfig(BaseModel):
    elements: list[NetworkElementConfig] = Field(default_factory=list)  # or premise_data.network
    value_streams: list[NetworkValueStreamConfig] = Field(default_factory=list)
    background_load: list[BackgroundLoadConfig] = Field(default_factory=list)


class HouseDefaultsConfig(BaseModel):
    """Applied to every house unless the house sets its own. Counterfactual policies
    are keyed by device name and apply to each house that has that device."""

    value_streams: list[ValueStreamConfig] = Field(default_factory=list)
    poi_limit_kw: float | None = None
    import_limit_kw: float | None = None  # overrides poi_limit_kw for import
    export_limit_kw: float | None = None  # overrides poi_limit_kw for export (0 = no export)
    exclusive_import_export: bool = False
    counterfactual: CounterfactualConfig | None = None
    # device behavior for houses built from premise_data, by device: battery, ev, solar
    # (e.g. {battery: {eta_charge: 0.96, degradation_cost_per_kwh: 0.015}}); sizes come
    # from the premises file
    device_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # who takes part in coordination, and how (docs/COORDINATION_SPEC.md section 1)
    participation: Literal["participant", "non_participant"] = "participant"
    baseline_behavior: Literal["own_bill", "rule_based"] = "own_bill"
    response: Literal["optimized", "rules"] = "optimized"
    rules: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)  # mechanism -> device -> action


class HouseConfig(BaseModel):
    """One house, written out: the same fields as a single-premise scenario, plus the
    network element it's served from."""

    name: str
    element: str
    devices: list[DeviceConfig] = Field(default_factory=list)
    external_load: list[float] | dict[str, Any]
    value_streams: list[ValueStreamConfig] | None = None
    poi_limit_kw: float | None = None
    import_limit_kw: float | None = None  # overrides poi_limit_kw for import
    export_limit_kw: float | None = None  # overrides poi_limit_kw for export (0 = no export)
    exclusive_import_export: bool | None = None
    counterfactual: CounterfactualConfig | None = None
    billing: BillingConfig | None = None  # this house's own; default the scenario's
    participation: Literal["participant", "non_participant"] | None = None  # default house_defaults'
    baseline_behavior: Literal["own_bill", "rule_based"] | None = None
    response: Literal["optimized", "rules"] | None = None
    rules: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)  # merged over house_defaults.rules
    envelope_weight: float | None = None  # envelopes, method weights


class PremiseDataConfig(BaseModel):
    """Houses and network from files in the input data spec (docs/INPUT_DATA_SPEC.md)."""

    premises: str  # premises.csv
    timeseries: list[str]  # one or more time series files; a premise's columns can span files
    network: str | None = None  # network.csv, instead of feeder.elements
    tz: str = "America/Los_Angeles"  # the local clock the run works on
    tariff_dir: str | None = None  # where premises' `tariff` names are looked up; default der-opt's example tariffs
    programs: dict[str, dict[str, Any]] = Field(default_factory=dict)  # nbt: {...}, nem2: {...}
    income_tier: int = 3
    baseline_code: str = "basic"


class SyntheticHousesConfig(BaseModel):
    """Generated houses on one element; see der_opt.network.synthetic. Devices are
    named solar, battery and ev."""

    element: str
    count: int
    seed: int = 0
    name_prefix: str | None = None  # default "<element>_h"
    solar_share: float = 0.4
    battery_share: float = 0.3  # of the solar homes
    ev_share: float = 0.4
    ev_v2x: bool = False
    t_min_c: float = 22.0
    t_max_c: float = 40.0


# --- feeder coordination (docs/COORDINATION_SPEC.md v1.0) -----------------------------


class PriceScheduleRule(BaseModel):
    element: str
    per_kwh: float
    months: list[int] | Literal["all"] = "all"
    hours: str = "00:00-24:00"  # local, "HH:MM-HH:MM"
    days: Literal["all", "weekdays", "weekends"] = "all"


class PriceSignalConfig(BaseModel):
    source: Literal["coordinated", "schedule", "file"] = "coordinated"
    schedule: list[PriceScheduleRule] = Field(default_factory=list)  # source: schedule
    path: str | None = None  # source: file -- timestamp, element_id, price_per_kwh
    paid: bool = True


class AllocationConfig(BaseModel):
    method: Literal["pro_rata_need", "equal", "service_size", "weights", "max_min", "fixed", "file"] = "pro_rata_need"
    column: str | None = None  # weights: a premises.csv column (else each house's envelope_weight)
    kw: float | None = None  # fixed
    path: str | None = None  # file -- timestamp, premise_id, import_kw, export_kw


class HeadroomConfig(BaseModel):
    margin_pct: float = 0.0


class EnvelopeConfig(BaseModel):
    import_: AllocationConfig | None = Field(default_factory=AllocationConfig, alias="import")
    export: AllocationConfig | None = None  # left out: no export envelope
    headroom: HeadroomConfig = Field(default_factory=HeadroomConfig)
    nesting: Literal["min_along_path", "top_down"] = "min_along_path"
    granularity: Literal["step", "static"] | dict[str, float] = "step"  # or {block_hours: 4}
    min_kw: float = 1.0
    violation_penalty_per_kwh: float = 5.0

    model_config = {"populate_by_name": True}


class FeederDRTrigger(BaseModel):
    above_pct: float


class FeederDRConfig(BaseModel):
    element: str
    program: DRProgramConfig
    trigger: FeederDRTrigger | None = None  # or events
    events: DREventsConfig | None = None
    duration: Literal["fixed", "variable"] = "variable"
    hours: float | None = None  # fixed duration; default the program's max_hours
    customer_class: Literal["residential", "commercial"] = "residential"
    tz: str = "America/Los_Angeles"


MechanismName = Literal["price_signal", "envelopes", "dr_events"]


class CoordinationConfig(BaseModel):
    price_signal: PriceSignalConfig | None = None
    envelopes: EnvelopeConfig | None = None
    dr_events: FeederDRConfig | None = None
    runs: dict[str, list[MechanismName]] | None = None  # left out: each alone, plus all combined


class FeederScenarioConfig(BaseModel):
    name: str
    time_grid: TimeGridConfig
    feeder: FeederConfig
    house_defaults: HouseDefaultsConfig = Field(default_factory=HouseDefaultsConfig)
    houses: list[HouseConfig] = Field(default_factory=list)
    synthetic_houses: list[SyntheticHousesConfig] = Field(default_factory=list)
    premise_data: PremiseDataConfig | None = None
    billing: BillingConfig | None = None
    coordination: CoordinationConfig | None = None
