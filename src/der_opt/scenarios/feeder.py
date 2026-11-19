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
        exclusive_import_export=(
            house.exclusive_import_export if house.exclusive_import_export is not None else defaults.exclusive_import_export
        ),
        counterfactual=counterfactual,
        billing=config.billing,
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


def build_feeder_scenario(config: FeederScenarioConfig) -> FeederScenario:
    generated, rows = _generated_houses(config)
    house_list = list(config.houses) + generated
    explicit_rows = [{"house": house.name, "synthetic": False} for house in config.houses]
    feeder = Feeder(
        config.name,
        [NetworkElement(e.name, e.rating_kw, e.overload_cost_per_kwh, e.parent) for e in config.feeder.elements],
        attachments={house.name: house.element for house in house_list},
    )
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
    notes: list[str] = field(default_factory=list)


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

    notes = []
    if counterfactual:
        policies = {
            premise.name: build_counterfactual_policies(house_config, premise)
            for house_config, premise in zip(scenario.house_configs, scenario.premises)
        }
        if any(p is not None for p in policies.values()):
            dispatches = {}
            for premise in scenario.premises:
                simulator = CounterfactualSimulator(premise, policies[premise.name] or {})
                dispatches[premise.name] = simulator.run()
                for device in premise.devices:
                    end = simulator.final_device_states.get(device.name, {}).get("soc")
                    floor = getattr(device, "soc_final_min", None)
                    if end is not None and floor is not None and end < floor - 1e-6:
                        notes.append(
                            f"{premise.name} {device.name} ends the counterfactual at {end:.1f} kWh, below the "
                            f"{floor:.1f} kWh the optimized runs must end with, which favors the counterfactual."
                        )
            runs["counterfactual"] = _summarize("counterfactual", scenario, network, dispatches)
    return FeederStudy(scenario, runs, prices, exact, notes)
