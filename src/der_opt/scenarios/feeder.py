"""Feeder scenarios: many premises on a distribution network, from YAML.

Each house -- written out under `houses:` or built from input-spec files under
`premise_data:` -- becomes an ordinary single-premise ScenarioConfig (sharing the scenario's time grid and
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

from der_opt.diagnostics import CheckReport, Finding
from der_opt.network import (
    CapacityDeferral,
    Feeder,
    NetworkDispatchModel,
    NetworkElement,
    element_flows,
)
from der_opt.premise import Premise
from der_opt.pricing import value_breakdown
from der_opt.results.schema import DispatchResult
from der_opt.scenarios.compose import compose_scenario, load_composed
from der_opt.scenarios.loader import (
    _build_time_grid,
    build_counterfactual_policies,
    build_premise,
)
from der_opt.scenarios.schema import (
    FeederScenarioConfig,
    ScenarioConfig,
)

# where premises' `tariff` names are looked up when premise_data.tariff_dir isn't set:
# the tariffs that ship with der-opt's examples
DEFAULT_TARIFF_DIR = Path(__file__).resolve().parents[3] / "examples" / "data" / "tariffs"


def is_feeder_scenario(path: str | Path) -> bool:
    return "feeder" in compose_scenario(path).data


def load_feeder_config(path: str | Path) -> FeederScenarioConfig:
    return FeederScenarioConfig.model_validate(load_composed(path).data)


@dataclass
class FeederScenario:
    config: FeederScenarioConfig
    feeder: Feeder
    house_configs: list[ScenarioConfig]
    premises: list[Premise]
    network_streams: list
    houses: pd.DataFrame  # one row per house: element, devices, sizes
    roles: dict = field(default_factory=dict)  # house -> coordination Role
    background: dict = field(default_factory=dict)  # name -> (element, kW): background load
    explicit_rules: dict = field(default_factory=dict)  # house -> rules as written on it
    resolved: list = field(default_factory=list)  # population.ResolvedPremise per house, with sources

    def network(self) -> NetworkDispatchModel:
        return NetworkDispatchModel(self.feeder, self.premises, self.network_streams, fixed_loads=self.background)


def build_feeder_scenario(config: FeederScenarioConfig) -> FeederScenario:
    """Every premise resolved (docs/POPULATION_MODEL_SPEC.md), then built like a one-house
    run, on the feeder's network."""
    from der_opt.population.compile import resolve_feeder

    resolved, file_elements, file_background = resolve_feeder(config)
    explicit = {house.name: house.rules or {} for house in config.houses}
    return assemble_feeder(config, resolved, file_elements, file_background, explicit)


def feeder_from_study(study) -> FeederScenario:
    """A study's feeder analysis: its population on its network
    (docs/POPULATION_MODEL_SPEC.md section 4)."""
    from der_opt.population.compile import resolve_study

    analysis = study.analyses.feeder
    resolved, file_elements, file_background = resolve_study(study)
    config = FeederScenarioConfig.model_validate({
        "name": study.name, "time_grid": study.time_grid.model_dump(),
        "feeder": {"elements": [e.model_dump() for e in analysis.elements],
                   "value_streams": [v.model_dump() for v in analysis.value_streams]},
        "coordination": analysis.coordination.model_dump(by_alias=True) if analysis.coordination else None,
    })
    explicit = {item.name: item.rules or {} for item in study.premises if item.kind == "premise"}
    return assemble_feeder(config, resolved, file_elements, file_background, explicit)


def assemble_feeder(config: FeederScenarioConfig, resolved, file_elements, file_background,
                    explicit_rules: dict) -> FeederScenario:
    """Resolved premises on the feeder's network, each built like a single-premise run."""
    from der_opt.population.compile import PopulationError
    from der_opt.population.report import population_problems
    from der_opt.scenarios.loader import _resolve_external_load

    problems = population_problems(resolved)
    if problems:
        raise PopulationError(problems)
    if file_elements is not None and config.feeder.elements:
        raise ValueError("network elements come from premise_data.network or feeder.elements, not both")
    elements = file_elements if file_elements is not None else [
        NetworkElement(e.name, e.rating_kw, e.overload_cost_per_kwh, e.parent, e.emergency_rating_kw,
                       e.violation_cost_per_kwh, e.emergency_kwh_per_day) for e in config.feeder.elements
    ]
    if not elements:
        raise ValueError("the feeder has no network elements (feeder.elements or premise_data.network)")
    names = [r.premise_id for r in resolved]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ValueError(f"premise names must be unique on a feeder; repeated: {repeated}")
    feeder = Feeder(config.name, elements, attachments={r.premise_id: r.element_id for r in resolved})
    house_configs = [r.to_scenario_config(config.time_grid) for r in resolved]
    premises = [build_premise(house_config) for house_config in house_configs]
    roles = {r.premise_id: r.role() for r in resolved}
    time_grid = _build_time_grid(config.time_grid)
    background = {}
    items = [(item.name, item.element, item.load) for item in config.feeder.background_load]
    items += [(name, element, load) for name, (element, load) in file_background.items()]  # kind: background rows
    for name, element, load in items:
        if name in names or name in background:
            raise ValueError(f"background_load {name!r}: the name is already used")
        feeder.element(element)  # fails early on a typo
        background[name] = (element, _resolve_external_load(load, time_grid))
    streams = [CapacityDeferral(s.element, s.value_per_kw, s.threshold_kw) for s in config.feeder.value_streams]
    for stream in streams:
        feeder.element(stream.element)  # fails early on a typo
    houses = pd.DataFrame([{"house": r.premise_id, **r.extra} for r in resolved]).set_index("house")
    houses.insert(0, "element", [r.element_id for r in resolved])
    houses.insert(1, "devices", [", ".join(d.device_id for d in r.devices) or "-" for r in resolved])
    houses.insert(2, "group", [roles[r.premise_id].group for r in resolved])
    houses.insert(3, "baseline_behavior", [roles[r.premise_id].baseline_behavior for r in resolved])
    return FeederScenario(config, feeder, house_configs, premises, streams, houses, roles, background, explicit_rules,
                          resolved)


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
    coordination: object | None = None  # coordination.study.CoordinationResult

    def coordination_table(self) -> pd.DataFrame:
        """One row per run: network loading and costs, customers' costs by group, the
        mechanisms' payments, and the share of the ideal captured."""
        from der_opt.coordination.study import share_of_ideal

        groups = self.scenario.houses["group"]
        mech = self.coordination.runs if self.coordination is not None else {}
        net = {label: sum(run.network_costs.values()) for label, run in self.runs.items()}
        rows = []
        for label, run in self.runs.items():
            row = {"run": label, "mechanisms": ", ".join(mech[label].mechanisms) if label in mech else ""}
            for element in self.scenario.feeder.elements:
                row[f"{element.name}_peak_kw"] = float(run.flows[element.name].max())
                if element.rating_kw:
                    over = (run.flows[element.name].abs() > element.rating_kw + 1e-6).sum()
                    row[f"{element.name}_hours_over"] = float(over * self.scenario.premises[0].time_grid.dt_hours)
            row["network_cost"] = net[label]
            for group in sorted(set(groups)):
                members = groups.index[groups == group]
                row[f"bills: {group}"] = float(run.house_costs.loc[members, "bill"].sum())
            row["degradation"] = float(run.house_costs["degradation"].sum())
            if label in mech and not mech[label].payments.empty:
                for column in mech[label].payments.columns:
                    row[column] = float(mech[label].payments[column].sum())
            if label in mech and {"independent", "coordinated"} <= set(net):
                row["network_saving"] = net["independent"] - net[label]
                row["ideal_network_saving"] = net["independent"] - net["coordinated"]
                row["share_of_ideal"] = share_of_ideal(net["independent"], net[label], net["coordinated"])
            rows.append(row)
        return pd.DataFrame(rows).set_index("run")

    def customers_table(self) -> pd.DataFrame:
        """One row per customer and run."""
        mech = self.coordination.runs if self.coordination is not None else {}
        frames = []
        for label, run in self.runs.items():
            frame = run.house_costs.copy()
            frame.insert(0, "group", self.scenario.houses["group"])
            if label in mech and not mech[label].payments.empty:
                frame = frame.join(mech[label].payments)
            frame.insert(0, "run", label)
            frames.append(frame.rename_axis("premise_id").reset_index())
        return pd.concat(frames, ignore_index=True)


def _house_costs(premises: list[Premise], dispatches: dict[str, DispatchResult]) -> pd.DataFrame:
    rows = {}
    for premise in premises:
        breakdown = value_breakdown(premise, dispatches[premise.name])
        degradation = sum(v for k, v in breakdown.items() if k.endswith(" degradation"))
        rows[premise.name] = {"bill": sum(breakdown.values()) - degradation, "degradation": degradation}
    return pd.DataFrame.from_dict(rows, orient="index")


def _summarize(label, scenario: FeederScenario, network: NetworkDispatchModel, dispatches) -> RunSummary:
    flows = element_flows(scenario.feeder, dispatches, scenario.background)
    return RunSummary(label, dispatches, flows, _house_costs(scenario.premises, dispatches), network.network_costs(flows))


def run_feeder_study(scenario: FeederScenario, independent: bool = True, counterfactual: bool = True) -> FeederStudy:
    """The coordinated, independent and counterfactual runs, then each coordination run
    (docs/COORDINATION_SPEC.md). Non-participants and background load are fixed flows
    in every run; participants are what the runs change."""
    from der_opt.coordination.study import baselines, make_network, run_coordination
    from der_opt.simulation import CounterfactualSimulator

    roles = scenario.roles
    participants = [p for p in scenario.premises if roles[p.name].participant]
    coordinating = scenario.config.coordination is not None
    needs_base = independent or coordinating or len(participants) < len(scenario.premises)
    base = baselines(scenario) if needs_base else {}
    fixed = dict(scenario.background)
    for premise in scenario.premises:
        if not roles[premise.name].participant:
            result = base[premise.name]
            fixed[premise.name] = (scenario.feeder.attachments[premise.name],
                                   (result.grid_import - result.grid_export).to_numpy())
    cost_network = scenario.network()
    index = pd.DatetimeIndex(scenario.premises[0].time_grid.timestamps())
    if participants:
        network = make_network(scenario, participants, fixed)
        result, model, backend = network.solve()
        prices, exact = network.congestion_prices(model, backend)
        coordinated = dict(result.premises)
    else:
        prices = pd.DataFrame(0.0, index=index, columns=[e.name for e in scenario.feeder.elements])
        exact, coordinated = True, {}
    coordinated |= {name: base[name] for name in fixed if name in base}
    runs = {"coordinated": _summarize("coordinated", scenario, cost_network, coordinated)}

    if independent or coordinating:
        runs["independent"] = _summarize("independent", scenario, cost_network, base)

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
            runs["counterfactual"] = _summarize("counterfactual", scenario, cost_network, dispatches)

    coordination = None
    if coordinating:
        coordination = run_coordination(scenario, base, fixed, prices, cost_network)
        for label, run in coordination.runs.items():
            runs[label] = _summarize(label, scenario, cost_network, run.dispatches)
    study = FeederStudy(scenario, runs, prices, exact, check_feeder_study(scenario, runs), coordination)
    return study


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
        dt = scenario.premises[0].time_grid.dt_hours
        emergency = []
        for element in scenario.feeder.elements:
            if element.rating_kw is None:
                continue
            peak = float(flows[element.name].abs().max())
            if element.emergency_rating_kw is not None:  # v1.1
                beyond = flows[element.name].abs() - element.emergency_rating_kw
                if element.violation_cost_per_kwh is None and beyond.max() > 1e-3:
                    found.append(Finding("error", "hard ratings hold", f"{element.name} exceeds its "
                                         f"{element.emergency_rating_kw:g} kW emergency rating (a hard wall) by "
                                         f"{beyond.max():.3f} kW", "coordinated"))
                elif beyond.max() > 1e-3:
                    emergency.append(Finding("warning", "emergency ratings", f"{element.name} exceeds its "
                                             f"{element.emergency_rating_kw:g} kW emergency rating in "
                                             f"{(beyond > 1e-3).sum() * dt:g} h, by up to {beyond.max():.2f} kW",
                                             "coordinated"))
                continue
            if element.overload_cost_per_kwh is not None:
                continue
            over = peak - element.rating_kw
            if over > 1e-3:
                found.append(Finding("error", "hard ratings hold", f"{element.name} exceeds its {element.rating_kw:g} kW "
                                     f"hard rating by {over:.3f} kW", "coordinated"))
        if any(e.emergency_rating_kw is not None for e in scenario.feeder.elements):
            report.ran("emergency ratings", emergency)
    report.ran("hard ratings hold", found)
    from der_opt.billing import load_retail_tariff

    for tariff_path in sorted({c.billing.tariff for c in scenario.house_configs if c.billing is not None}):
        report.extend(check_tariff_dates(load_retail_tariff(tariff_path), scenario.premises[0].time_grid))
    return report
