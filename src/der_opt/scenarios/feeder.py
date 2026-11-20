"""Feeder scenarios: many houses on a distribution network, from YAML.

Each house -- written out under `houses:` or generated under `synthetic_houses:` --
becomes an ordinary single-premise ScenarioConfig (sharing the scenario's time grid and
billing), so devices, bills and counterfactual policies are built by the same code as
a one-house run. run_feeder_study then compares up to three dispatches of the same
houses on the same network:

  coordinated   one optimization over every house and the network limits
  independent   each house optimizing its own bill, blind to the network
  counterfactual  rule-based control (e.g. EVs charging on plug-in), if configured
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from der_opt.diagnostics import CheckReport, Finding
from der_opt.network import (
    CapacityDeferral,
    Feeder,
    NetworkDispatchModel,
    NetworkElement,
    element_flows,
    solve_independently,
)
from der_opt.network.synthetic import synthetic_houses
from der_opt.premise import Premise
from der_opt.pricing import value_breakdown
from der_opt.results.schema import DispatchResult
from der_opt.scenarios.loader import (
    _build_time_grid,
    build_counterfactual_policies,
    build_premise,
)
from der_opt.scenarios.schema import (
    BillingConfig,
    CounterfactualConfig,
    DeviceConfig,
    FeederScenarioConfig,
    HouseConfig,
    ScenarioConfig,
)


def is_feeder_scenario(path: str | Path) -> bool:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return isinstance(raw, dict) and "feeder" in raw


def load_feeder_config(path: str | Path) -> FeederScenarioConfig:
    return FeederScenarioConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass
class FeederScenario:
    config: FeederScenarioConfig
    feeder: Feeder
    house_configs: list[ScenarioConfig]
    premises: list[Premise]
    network_streams: list
    houses: pd.DataFrame  # one row per house: element, devices, synthetic draws

    def network(self) -> NetworkDispatchModel:
        return NetworkDispatchModel(self.feeder, self.premises, self.network_streams)


def _house_scenario(config: FeederScenarioConfig, house: HouseConfig) -> ScenarioConfig:
    defaults = config.house_defaults
    device_names = {device.name for device in house.devices}
    counterfactual = house.counterfactual
    if counterfactual is None and defaults.counterfactual is not None:
        policies = {name: policy for name, policy in defaults.counterfactual.policies.items() if name in device_names}
        counterfactual = CounterfactualConfig(policies=policies)
    return ScenarioConfig(
        name=house.name,
        time_grid=config.time_grid,
        devices=house.devices,
        value_streams=house.value_streams if house.value_streams is not None else defaults.value_streams,
        external_load=house.external_load,
        poi_limit_kw=house.poi_limit_kw if house.poi_limit_kw is not None else defaults.poi_limit_kw,
        import_limit_kw=house.import_limit_kw if house.import_limit_kw is not None else defaults.import_limit_kw,
        export_limit_kw=house.export_limit_kw if house.export_limit_kw is not None else defaults.export_limit_kw,
        exclusive_import_export=(
            house.exclusive_import_export if house.exclusive_import_export is not None else defaults.exclusive_import_export
        ),
        counterfactual=counterfactual,
        billing=house.billing if house.billing is not None else config.billing,
    )


def _generated_houses(config: FeederScenarioConfig) -> tuple[list[HouseConfig], list[dict]]:
    time_grid = _build_time_grid(config.time_grid)
    house_configs, rows = [], []
    for group in config.synthetic_houses:
        generated = synthetic_houses(
            time_grid, group.count, seed=group.seed, name_prefix=group.name_prefix or f"{group.element}_h",
            solar_share=group.solar_share, battery_share=group.battery_share, ev_share=group.ev_share,
            ev_v2x=group.ev_v2x, t_min_c=group.t_min_c, t_max_c=group.t_max_c,
        )
        for house in generated:
            devices = []
            if house.solar:
                devices.append(DeviceConfig(type="solar", name="solar", params=house.solar))
            if house.battery:
                devices.append(DeviceConfig(type="battery", name="battery", params=house.battery | {"charge_sources": ["solar"]}))
            if house.ev:
                devices.append(DeviceConfig(type="ev_v2x", name="ev", params=house.ev))
            house_configs.append(HouseConfig(name=house.name, element=group.element, devices=devices, external_load=house.load_kw.tolist()))
            rows.append({"house": house.name, "synthetic": True, "peak_load_kw": round(float(house.load_kw.max()), 2), **house.traits})
    return house_configs, rows


# behavior defaults for houses built from premise_data; sizes come from the premises
# file and anything here can be overridden in house_defaults.device_params
DEVICE_DEFAULTS = {
    "battery": {"eta_charge": 0.95, "eta_discharge": 0.95, "soc_init_frac": 0.5, "soc_final_min_frac": 0.5},
    # charge to 90% by every departure, starting the run at 90%
    "ev": {"eta_charge": 0.92, "eta_discharge": 0.92, "soc_max_frac": 0.9, "soc_init_frac": 0.9,
           "soc_departure_min_frac": 0.9},
    "solar": {},
}


def _premise_houses(config: FeederScenarioConfig) -> tuple[list[HouseConfig], list[dict], list[NetworkElement] | None]:
    """Houses (and optionally the network) from input-spec files."""
    from der_opt.inputs import read_network, read_premises, read_timeseries

    data = config.premise_data
    if data is None:
        return [], [], None
    network = read_network(data.network) if data.network else None
    premises = read_premises(data.premises, network)
    series = [read_timeseries(path) for path in data.timeseries]
    time_grid = _build_time_grid(config.time_grid)
    params = {kind: DEVICE_DEFAULTS[kind] | config.house_defaults.device_params.get(kind, {}) for kind in DEVICE_DEFAULTS}

    def source(premise: str, column: str) -> dict | None:
        """The time series file holding this premise's column, as a data source."""
        for path, ts in zip(data.timeseries, series):
            if ts.premises is None or premise not in ts.premises or column not in ts.columns:
                continue
            rows = ts.data[ts.data["premise_id"] == premise]
            if rows[column].notna().any():
                return {"source": "timeseries", "path": path, "premise_id": premise, "column": column, "tz": data.tz}
        return None

    def number(row, column):
        value = row.get(column)
        return None if value is None or pd.isna(value) else float(value)

    houses, rows, problems = [], [], []
    for premise, row in premises.table.iterrows():
        element = row.get("element_id") or None
        if element is None:
            problems.append(f"{premise}: no element_id (every premise on a feeder needs one)")
            continue
        load, imported = source(premise, "load_kw"), source(premise, "import_kw")
        devices = []
        sizes = {c: number(row, c) for c in ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw")}
        if imported is not None:
            external = imported | {"column": "net_kw"}
            if any(v is not None for v in sizes.values()):
                problems.append(f"{premise}: metered data (import_kw/export_kw) already includes its devices, so "
                                "it can't also have solar, battery or EV sizes")
                continue
        elif load is not None:
            external = load
        else:
            problems.append(f"{premise}: no load_kw or import_kw time series in {data.timeseries}")
            continue

        pv = source(premise, "pv_kw")
        if pv is not None:
            devices.append(DeviceConfig(type="solar", name="solar", params=params["solar"] | {"available_kw": pv}))
        elif sizes["pv_kw_ac"] is not None:
            problems.append(f"{premise}: pv_kw_ac is set but there's no pv_kw time series for it")
            continue
        if sizes["battery_kwh"] is not None:
            battery = params["battery"] | {"capacity_kwh": sizes["battery_kwh"], "p_max_charge_kw": sizes["battery_kw"],
                                           "p_max_discharge_kw": sizes["battery_kw"]}
            if "charge_sources" in battery and pv is None:
                battery.pop("charge_sources")
            devices.append(DeviceConfig(type="battery", name="battery", params=battery))
        if sizes["ev_battery_kwh"] is not None:
            connected, driving = source(premise, "ev_connected"), source(premise, "ev_driving_kwh")
            if connected is None or driving is None:
                problems.append(f"{premise}: an EV needs ev_connected and ev_driving_kwh time series")
                continue
            from der_opt.scenarios.data_sources import resolve_array_param

            plugged = resolve_array_param(connected, time_grid)
            departures = [t for t in range(1, len(plugged)) if plugged[t - 1] > 0.5 and plugged[t] <= 0.5]
            v2x = str(row.get("ev_v2x") or "0") == "1"
            ev = params["ev"] | {
                "capacity_kwh": sizes["ev_battery_kwh"], "p_max_charge_kw": sizes["ev_charger_kw"],
                "p_max_discharge_kw": sizes["ev_charger_kw"] if v2x else 0.0,
                "connected": [float(x) for x in plugged], "driving_kwh": driving, "departure_period": departures,
            }
            devices.append(DeviceConfig(type="ev_v2x", name="ev", params=ev))

        billing = None
        if row.get("tariff"):
            program = row.get("export_program") or None
            billing = BillingConfig(
                tariff=str(Path(data.tariff_dir) / f"{row['tariff']}.yaml"), income_tier=data.income_tier,
                program=({"type": program} | data.programs.get(program, {})) if program else None,
                baseline_territory=row.get("baseline_territory") or None, baseline_code=data.baseline_code,
            )
        houses.append(HouseConfig(
            name=premise, element=element, devices=devices, external_load=external, billing=billing,
            import_limit_kw=number(row, "import_limit_kw"), export_limit_kw=number(row, "export_limit_kw"),
        ))
        rows.append({"house": premise, "synthetic": False, "data": "metered" if imported else "gross",
                     **{k: v for k, v in sizes.items() if v is not None}, "tariff": row.get("tariff") or "",
                     "export_program": row.get("export_program") or ""})
    if problems:
        raise ValueError("premise_data problems:\n" + "\n".join(f"  - {p}" for p in problems))

    elements = None
    if network is not None:
        elements = [
            NetworkElement(element, number(r, "rating_kw"), number(r, "overload_cost_per_kwh"), r["parent_id"] or None)
            for element, r in network.table.iterrows()
        ]
    return houses, rows, elements


def build_feeder_scenario(config: FeederScenarioConfig) -> FeederScenario:
    generated, rows = _generated_houses(config)
    from_files, file_rows, file_elements = _premise_houses(config)
    house_list = list(config.houses) + generated + from_files
    rows = rows + file_rows
    explicit_rows = [{"house": house.name, "synthetic": False} for house in config.houses]
    if file_elements is not None and config.feeder.elements:
        raise ValueError("network elements come from premise_data.network or feeder.elements, not both")
    elements = file_elements if file_elements is not None else [
        NetworkElement(e.name, e.rating_kw, e.overload_cost_per_kwh, e.parent) for e in config.feeder.elements
    ]
    if not elements:
        raise ValueError("the feeder has no network elements (feeder.elements or premise_data.network)")
    feeder = Feeder(config.name, elements, attachments={house.name: house.element for house in house_list})
    house_configs = [_house_scenario(config, house) for house in house_list]
    premises = [build_premise(house_config) for house_config in house_configs]
    streams = [CapacityDeferral(s.element, s.value_per_kw, s.threshold_kw) for s in config.feeder.value_streams]
    for stream in streams:
        feeder.element(stream.element)  # fails early on a typo
    houses = pd.DataFrame(explicit_rows + rows).set_index("house").loc[[h.name for h in house_list]]
    houses.insert(0, "element", [house.element for house in house_list])
    houses.insert(1, "devices", [", ".join(d.name for d in house.devices) or "-" for house in house_list])
    return FeederScenario(config, feeder, house_configs, premises, streams, houses)


@dataclass
class RunSummary:
    """One dispatch of every house, priced the same way whichever way it was made."""

    label: str
    dispatches: dict[str, DispatchResult]
    flows: pd.DataFrame
    house_costs: pd.DataFrame  # bill and degradation per house
    network_costs: dict[str, float]

    @property
    def total(self) -> float:
        return float(self.house_costs.sum().sum()) + sum(self.network_costs.values())


@dataclass
class FeederStudy:
    scenario: FeederScenario
    runs: dict[str, RunSummary]
    congestion_prices: pd.DataFrame
    prices_exact: bool
    checks: CheckReport = field(default_factory=CheckReport)


def _house_costs(premises: list[Premise], dispatches: dict[str, DispatchResult]) -> pd.DataFrame:
    rows = {}
    for premise in premises:
        breakdown = value_breakdown(premise, dispatches[premise.name])
        degradation = sum(v for k, v in breakdown.items() if k.endswith(" degradation"))
        rows[premise.name] = {"bill": sum(breakdown.values()) - degradation, "degradation": degradation}
    return pd.DataFrame.from_dict(rows, orient="index")


def _summarize(label, scenario: FeederScenario, network: NetworkDispatchModel, dispatches) -> RunSummary:
    flows = element_flows(scenario.feeder, dispatches)
    return RunSummary(label, dispatches, flows, _house_costs(scenario.premises, dispatches), network.network_costs(flows))


def run_feeder_study(scenario: FeederScenario, independent: bool = True, counterfactual: bool = True) -> FeederStudy:
    from der_opt.simulation import CounterfactualSimulator

    network = scenario.network()
    result, model, backend = network.solve()
    prices, exact = network.congestion_prices(model, backend)
    runs = {"coordinated": _summarize("coordinated", scenario, network, result.premises)}

    if independent:
        runs["independent"] = _summarize("independent", scenario, network, solve_independently(scenario.premises))

    if counterfactual:
        policies = {
            premise.name: build_counterfactual_policies(house_config, premise)
            for house_config, premise in zip(scenario.house_configs, scenario.premises)
        }
        if any(p is not None for p in policies.values()):
            dispatches = {
                premise.name: CounterfactualSimulator(premise, policies[premise.name] or {}).run()
                for premise in scenario.premises
            }
            runs["counterfactual"] = _summarize("counterfactual", scenario, network, dispatches)
    return FeederStudy(scenario, runs, prices, exact, check_feeder_study(scenario, runs))


def check_feeder_study(scenario: FeederScenario, runs: dict[str, RunSummary]) -> CheckReport:
    """The single-premise checks on every house in every run, plus the network's own:
    hard ratings must hold in the coordinated run."""
    from der_opt.diagnostics import (
        check_dispatch,
        check_end_states,
        check_load,
        check_prices,
        check_tariff_dates,
    )

    report = CheckReport()
    for premise in scenario.premises:
        report.extend(check_prices(premise, name=premise.name))
        report.extend(check_load(premise, name=premise.name))
        for label, run in runs.items():
            report.extend(check_dispatch(premise, run.dispatches[premise.name], run=label, name=premise.name))
        report.extend(check_end_states(premise, {label: run.dispatches[premise.name] for label, run in runs.items()},
                                       name=premise.name))
    found = []
    if "coordinated" in runs:
        flows = runs["coordinated"].flows
        for element in scenario.feeder.elements:
            if element.rating_kw is None or element.overload_cost_per_kwh is not None:
                continue
            over = float(flows[element.name].abs().max()) - element.rating_kw
            if over > 1e-3:
                found.append(Finding("error", "hard ratings hold", f"{element.name} exceeds its {element.rating_kw:g} kW "
                                     f"hard rating by {over:.3f} kW", "coordinated"))
    report.ran("hard ratings hold", found)
    from der_opt.billing import load_retail_tariff

    for tariff_path in sorted({c.billing.tariff for c in scenario.house_configs if c.billing is not None}):
        report.extend(check_tariff_dates(load_retail_tariff(tariff_path), scenario.premises[0].time_grid))
    return report
