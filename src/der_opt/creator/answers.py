"""What the scenario creator's steps collect (docs/SCENARIO_CREATOR_SPEC.md v1.0,
section 1): plain data, so the app can hold it as JSON and the writer can turn it into a
scenario or study file. A value left out (None) is written as TODO where the file needs
one, so `der-opt validate` lists it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["home", "portfolio", "feeder", "both"]
STEPS = {  # number: (key, title)
    1: ("what", "What"), 2: ("when", "When"), 3: ("data", "Data"), 4: ("network", "Network"),
    5: ("devices", "Devices"), 6: ("prices", "Prices and bill"), 7: ("meter", "Meter"),
    8: ("behavior", "Today's behavior"), 9: ("participation", "Who takes part"), 10: ("dr", "DR program"),
    11: ("coordination", "Coordination"), 12: ("value", "Value and cost tests"), 13: ("review", "Review"),
}
FEEDER_ONLY = (4, 9, 11)


def steps_for(kind: Kind) -> list[int]:
    return [n for n in STEPS if kind in ("feeder", "both") or n not in FEEDER_ONLY]


class When(BaseModel):
    year: int | None = None  # a whole calendar year, hourly
    start: str | None = None  # or a window: "2026-07-13T12:00:00"
    steps: int | None = None
    resolution_minutes: int = 60


class Data(BaseModel):
    timeseries: str | None = None  # a home: one file; a study: the first of `timeseries_files`
    premise: str | None = None
    premises: str | None = None  # a study's files (input spec 2.0)
    devices: str | None = None
    timeseries_files: list[str] = Field(default_factory=list)
    network: str | None = None
    tz: str = "America/Los_Angeles"


class Device(BaseModel):
    """A home's device: from a library block (`template`, a block stem in devices/) with
    its settings changed where they differ, or written out in full."""

    type: Literal["solar", "battery", "thermostat", "curtailable_load", "ev_v2x"]
    name: str
    template: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)  # beside the template's, or all of them


class AddDevices(BaseModel):
    """A study's devices added to a share of homes (an add_devices entry)."""

    where: dict[str, Any] = Field(default_factory=dict)
    share: float = 1.0
    seed: int = 1
    device: dict[str, Any] = Field(default_factory=dict)


class Prices(BaseModel):
    objective: Literal["bill", "arbitrage", "lmp"] = "bill"
    tariff: str | None = None  # a tariff YAML (a home), or the tariffs folder (a study)
    program: Literal["nbt", "nem2", "none"] = "nbt"
    export_rates: str | None = None  # NBT export rates CSV
    acc_plus_per_kwh: float = 0.0088
    nsc_rate_per_kwh: float = 0.02781
    baseline_territory: str | None = None
    income_tier: int = 3
    export_price: float = 0.05  # arbitrage: a flat export price
    price: str | None = None  # lmp: a price CSV (timestamp, lmp_per_kwh)
    billed: bool = True  # a home: bill it on the tariff (a billing section), whatever it optimizes


class Meter(BaseModel):
    poi_limit_kw: float | None = 20.0
    import_limit_kw: float | None = None
    export_limit_kw: float | None = None
    single_meter: bool = False


class Element(BaseModel):
    name: str
    rating_kw: float | None = None
    parent: str | None = None
    overload_cost_per_kwh: float | None = None
    emergency_rating_kw: float | None = None
    violation_cost_per_kwh: float | None = None
    emergency_kwh_per_day: float | None = None


class Background(BaseModel):
    name: str
    element: str
    load: dict[str, Any]  # a data source: {source: timeseries, ...} or {source: loadcast, path, trace, var}


class Network(BaseModel):
    elements: list[Element] = Field(default_factory=list)  # empty: from data.network
    background: list[Background] = Field(default_factory=list)


class Participation(BaseModel):
    participation: Literal["participant", "non_participant"] = "participant"
    baseline_behavior: Literal["own_bill", "rule_based"] = "own_bill"
    response: Literal["optimized", "rules"] = "optimized"
    rules: dict[str, dict[str, Any]] = Field(default_factory=dict)  # mechanism -> {device type: action}


class Coordination(BaseModel):
    price_signal: dict[str, Any] | None = None
    envelopes: dict[str, Any] | None = None
    dr_events: dict[str, Any] | None = None
    runs: dict[str, list[str]] | None = None


class Value(BaseModel):
    avoided_costs: str | None = None  # screening values
    cost_test: dict[str, Any] | None = None  # the cost_test section
    deferral: list[dict[str, Any]] = Field(default_factory=list)  # [{element, value_per_kw, threshold_kw}]


class Answers(BaseModel):
    kind: Kind = "home"
    name: str = "new_scenario"
    when: When = Field(default_factory=When)
    data: Data = Field(default_factory=Data)
    network: Network = Field(default_factory=Network)
    devices: list[Device] = Field(default_factory=list)  # a home's
    solar_from_data: bool = True  # a home: solar from the time series' pv_kw
    device_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)  # a study: by type, as defaults
    add_devices: list[AddDevices] = Field(default_factory=list)
    prices: Prices = Field(default_factory=Prices)
    meter: Meter = Field(default_factory=Meter)
    behavior: dict[str, str] = Field(default_factory=dict)  # device name (home) or type (study) -> policy
    participation: Participation = Field(default_factory=Participation)
    dr: dict[str, Any] | None = None  # the dr section (a home) or the portfolio's
    coordination: Coordination = Field(default_factory=Coordination)
    value: Value = Field(default_factory=Value)

    @property
    def study(self) -> bool:
        return self.kind != "home"

    @property
    def feeder(self) -> bool:
        return self.kind in ("feeder", "both")

    @property
    def portfolio(self) -> bool:
        return self.kind in ("portfolio", "both")
