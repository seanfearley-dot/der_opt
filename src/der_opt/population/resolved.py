"""The resolved premise (docs/POPULATION_MODEL_SPEC.md section 5): what every way of
describing a premise compiles to, with every setting's value and where it came from.
The engine builds from these, never from YAML fields or table columns directly."""

from __future__ import annotations

from dataclasses import dataclass, field

from der_opt.population.templates import KINDS

# premise-level settings a scenario carries
PREMISE_SETTINGS = ("external_load", "value_streams", "poi_limit_kw", "import_limit_kw", "export_limit_kw",
                    "exclusive_import_export", "billing", "dr", "avoided_costs", "cost_test", "weather_case")
ROLE_SETTINGS = ("participation", "baseline_behavior", "response", "envelope_weight")
BUILT_IN_PREMISE = {"value_streams": [], "exclusive_import_export": False, "participation": "participant",
                    "baseline_behavior": "own_bill", "response": "optimized"}


@dataclass
class ResolvedDevice:
    device_id: str
    type: str  # solar, battery, ev_v2x, thermostat, curtailable_load
    params: dict
    sources: dict[str, str]  # parameter (dotted path) -> source
    template: str | None = None
    counterfactual: dict | None = None  # its R1 policy, e.g. {type: self_consumption}
    counterfactual_source: str | None = None
    rules: dict = field(default_factory=dict)  # mechanism -> rule
    rule_sources: dict = field(default_factory=dict)  # mechanism -> source
    costs: dict = field(default_factory=dict)  # installed_cost, annual_om (to the participant)

    @property
    def kind(self) -> str:
        return KINDS[self.type]


@dataclass
class ResolvedPremise:
    premise_id: str
    origin: str  # "scenario", "houses", "premises.csv"
    settings: dict  # PREMISE_SETTINGS and ROLE_SETTINGS
    sources: dict[str, str]
    devices: list[ResolvedDevice]
    element_id: str | None = None
    has_counterfactual: bool = False  # whether it has a counterfactual (R1) run at all
    extra: dict = field(default_factory=dict)  # descriptive columns for houses.csv

    def to_scenario_config(self, time_grid):
        """The single-premise scenario the engine builds from."""
        from der_opt.scenarios.schema import (
            CounterfactualConfig,
            DeviceConfig,
            ScenarioConfig,
        )

        policies = {d.device_id: d.counterfactual for d in self.devices if d.counterfactual is not None}
        values = {k: self.settings[k] for k in PREMISE_SETTINGS if self.settings.get(k) is not None}
        return ScenarioConfig.model_validate({
            "name": self.premise_id,
            "time_grid": time_grid.model_dump() if hasattr(time_grid, "model_dump") else time_grid,
            "devices": [DeviceConfig(type=d.type, name=d.device_id, params=d.params).model_dump() for d in self.devices],
            "counterfactual": CounterfactualConfig(policies=policies).model_dump() if self.has_counterfactual else None,
            **values,
        })

    def role(self):
        from der_opt.coordination.roles import Role

        rules: dict = {}
        for device in self.devices:
            for mechanism, rule in device.rules.items():
                rules.setdefault(mechanism, {})[device.device_id] = rule
        return Role(self.settings.get("participation", "participant"),
                    self.settings.get("baseline_behavior", "own_bill"),
                    self.settings.get("response", "optimized"), rules, self.settings.get("envelope_weight"))

    def source_of(self, setting: str) -> str:
        """Where a setting came from; for a mapping set field by field, each field's
        source, as `field: source` when they differ."""
        if setting in self.sources:
            return self.sources[setting]
        leaves = {k[len(setting) + 1:]: v for k, v in self.sources.items() if k.startswith(setting + ".")}
        if not leaves:
            return "built-in"
        if len(set(leaves.values())) == 1:
            return next(iter(leaves.values()))
        return "; ".join(f"{k}: {v}" for k, v in sorted(leaves.items()))

    def parameter_rows(self) -> list[dict]:
        """One row per setting: premise settings, then each device's parameters."""
        rows = [{"premise_id": self.premise_id, "device_id": "", "setting": k, "value": _show(self.settings.get(k)),
                 "source": self.source_of(k)} for k in (*PREMISE_SETTINGS, *ROLE_SETTINGS, "customer_class")
                if self.settings.get(k) is not None]
        for device in self.devices:
            for path, source in sorted(device.sources.items()):
                rows.append({"premise_id": self.premise_id, "device_id": device.device_id, "setting": path,
                             "value": _show(_get(device.params, path)), "source": source})
            if device.counterfactual is not None:
                rows.append({"premise_id": self.premise_id, "device_id": device.device_id, "setting": "counterfactual",
                             "value": _show(device.counterfactual), "source": device.counterfactual_source})
            for mechanism, rule in sorted(device.rules.items()):
                rows.append({"premise_id": self.premise_id, "device_id": device.device_id,
                             "setting": f"rules.{mechanism}", "value": _show(rule),
                             "source": device.rule_sources.get(mechanism)})
        return rows


def _get(values: dict, path: str):
    for key in path.split("."):
        if not isinstance(values, dict) or key not in values:
            return None
        values = values[key]
    return values


def _show(value) -> str:
    if isinstance(value, list) and len(value) > 6:
        return f"[{len(value)} values]"
    if hasattr(value, "tolist"):
        return f"[{len(value)} values]"
    return str(value)
