"""Today's three ways of describing a premise, compiled to resolved premises
(docs/POPULATION_MODEL_SPEC.md sections 5-6): a single-home scenario, a feeder's
written-out `houses:`, and `premises.csv` rows under `premise_data`.

Each setting is resolved by the one precedence rule (built-in, template, defaults, row,
override). What were special cases are layers:
  house_defaults (and the scenario's billing)       -> defaults, for every house
  house_defaults.device_params.<type>               -> defaults, for premises.csv devices
  the v1.1 premises device defaults (DEVICE_DEFAULTS) -> built-in, for premises.csv devices
  house_defaults.counterfactual, by device name     -> defaults, per device
  house_defaults.rules, a house's rules             -> defaults and row, per device
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from der_opt.population.precedence import Layer, resolve
from der_opt.population.resolved import (
    BUILT_IN_PREMISE,
    PREMISE_SETTINGS,
    ROLE_SETTINGS,
    ResolvedDevice,
    ResolvedPremise,
)
from der_opt.population.templates import KINDS, Template, load_template

# behavior defaults for devices from v1.1 premises files; sizes come from the file
DEVICE_DEFAULTS = {
    "battery": {"eta_charge": 0.95, "eta_discharge": 0.95, "soc_init_frac": 0.5, "soc_final_min_frac": 0.5},
    # charge to 90% by every departure, starting the run at 90%
    "ev": {"eta_charge": 0.92, "eta_discharge": 0.92, "soc_max_frac": 0.9, "soc_init_frac": 0.9,
           "soc_departure_min_frac": 0.9},
    "solar": {},
}


def _set_fields(model, names) -> dict:
    """The fields of a config that were written (not left at their defaults)."""
    return {k: _plain(getattr(model, k)) for k in names if k in model.model_fields_set and getattr(model, k) is not None}


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _rule_merge(current: dict | None, rule: dict | None) -> dict | None:
    """The same action merges settings; another action replaces the rule."""
    if rule is None:
        return None
    if current is not None and rule.get("action", current.get("action")) == current.get("action"):
        return {**current, **rule}
    return dict(rule)


def _rules_for(device_id: str, kind: str, layers: list[tuple[str, str, dict]]) -> tuple[dict, dict]:
    """Each mechanism's rule for one device: per layer, a rule by the device's name beats
    one by its type; across layers, the stronger layer wins."""
    from der_opt.coordination.roles import DEFAULT_RULES, MECHANISM_RULES

    rules, sources = {}, {}
    for mechanism in MECHANISM_RULES:
        current, source = DEFAULT_RULES.get(mechanism, {}).get(kind), "built-in"
        for _level, label, by_mechanism in layers:
            entries = (by_mechanism or {}).get(mechanism) or {}
            if device_id in entries:
                current, source = _rule_merge(current, entries[device_id]), label
            elif kind in entries:
                current, source = _rule_merge(current, entries[kind]), label
        if current is not None and current.get("action") != "none":
            rules[mechanism], sources[mechanism] = current, source
    return rules, sources


def _device(device_id: str, kind_type: str, layers: list[Layer], template: Template | None,
            policies: list[tuple[str, dict | None]], rule_layers: list[tuple[str, str, dict]]) -> ResolvedDevice:
    if template is not None:
        if template.type != kind_type:
            raise ValueError(f"{device_id}: template {template.name} is for a {template.type}, not a {kind_type}")
        layers = [Layer("template", template.source, template.params), *layers]
        if template.counterfactual is not None:
            policies = [(template.source, template.counterfactual), *policies]
        rule_layers = [("template", template.source, {m: {device_id: r} for m, r in template.rules.items()}),
                       *rule_layers]
    params, sources = resolve(layers)
    policy, policy_source = None, None
    for source, value in policies:
        if value is not None:
            policy, policy_source = value, source
    rules, rule_sources = _rules_for(device_id, KINDS[kind_type], rule_layers)
    return ResolvedDevice(device_id, kind_type, params, sources, template.name if template else None,
                          policy, policy_source, rules, rule_sources)


def _template(device) -> Template | None:
    return load_template(device.template) if getattr(device, "template", None) else None


def _check_policy_names(label: str, policies: dict, devices) -> None:
    names = {d.name for d in devices}
    unknown = sorted(set(policies) - names)
    if unknown:
        raise ValueError(f"{label}: counterfactual policy for unknown device {unknown[0]!r}; devices are {sorted(names)}")


def resolve_scenario(config) -> ResolvedPremise:
    """A single-home scenario: everything it writes is the premise's own (row) setting."""
    names = (*PREMISE_SETTINGS,)
    settings, sources = resolve([Layer("built-in", "built-in", dict(BUILT_IN_PREMISE)),
                                 Layer("row", "scenario", _set_fields(config, names))])
    own_policies = config.counterfactual.policies if config.counterfactual is not None else {}
    _check_policy_names(config.name, own_policies, config.devices)
    devices = [
        _device(d.name, d.type, [Layer("row", "scenario", dict(d.params))], _template(d),
                [("scenario", own_policies.get(d.name))], [])
        for d in config.devices
    ]
    has_cf = config.counterfactual is not None or any(d.counterfactual is not None for d in devices)
    return ResolvedPremise(config.name, "scenario", settings, sources, devices, has_counterfactual=has_cf)


def _house_layers(config, house_fields: dict, label: str) -> tuple[dict, dict]:
    defaults = config.house_defaults
    layers = [Layer("built-in", "built-in", dict(BUILT_IN_PREMISE))]
    default_values = _set_fields(defaults, ("value_streams", "poi_limit_kw", "import_limit_kw", "export_limit_kw",
                                            "exclusive_import_export", *ROLE_SETTINGS[:3]))
    if config.billing is not None:
        layers.append(Layer("defaults", "billing", {"billing": _plain(config.billing)}))
    layers.append(Layer("defaults", "house_defaults", default_values))
    layers.append(Layer("row", label, house_fields))
    return resolve(layers)


def _house(config, house) -> ResolvedPremise:
    defaults = config.house_defaults
    fields = _set_fields(house, ("external_load", "value_streams", "poi_limit_kw", "import_limit_kw",
                                 "export_limit_kw", "exclusive_import_export", "billing", *ROLE_SETTINGS))
    settings, sources = _house_layers(config, fields, f"houses.{house.name}")
    default_policies = defaults.counterfactual.policies if defaults.counterfactual is not None else {}
    own_policies = house.counterfactual.policies if house.counterfactual is not None else {}
    _check_policy_names(house.name, own_policies, house.devices)
    rule_layers = [("defaults", "house_defaults.rules", defaults.rules),
                   ("row", f"houses.{house.name}.rules", house.rules)]
    devices = [
        _device(d.name, d.type, [Layer("row", f"houses.{house.name}", dict(d.params))], _template(d),
                [("house_defaults.counterfactual", default_policies.get(d.name)),
                 (f"houses.{house.name}.counterfactual", own_policies.get(d.name))], rule_layers)
        for d in house.devices
    ]
    has_cf = (house.counterfactual is not None or defaults.counterfactual is not None
              or any(d.counterfactual is not None for d in devices))
    return ResolvedPremise(house.name, "houses", settings, sources, devices, element_id=house.element,
                           has_counterfactual=has_cf, extra={"data": "written out"})


def _number(row, column):
    value = row.get(column)
    return None if value is None or pd.isna(value) else float(value)


def _premise_rows(config):
    """Premises (and optionally the network) from v1.1 input-spec files."""
    from der_opt.inputs import read_network, read_premises, read_timeseries
    from der_opt.network import NetworkElement
    from der_opt.scenarios.data_sources import resolve_array_param
    from der_opt.scenarios.feeder import DEFAULT_TARIFF_DIR
    from der_opt.scenarios.loader import _build_time_grid

    data = config.premise_data
    if data is None:
        return [], None
    defaults = config.house_defaults
    network = read_network(data.network) if data.network else None
    premises = read_premises(data.premises, network)
    series = [read_timeseries(path) for path in data.timeseries]
    time_grid = _build_time_grid(config.time_grid)
    default_policies = defaults.counterfactual.policies if defaults.counterfactual is not None else {}

    def source(premise: str, column: str) -> dict | None:
        """The time series file holding this premise's column, as a data source."""
        for path, ts in zip(data.timeseries, series, strict=True):
            if ts.premises is None or premise not in ts.premises or column not in ts.columns:
                continue
            rows = ts.data[ts.data["premise_id"] == premise]
            if rows[column].notna().any():
                return {"source": "timeseries", "path": path, "premise_id": premise, "column": column, "tz": data.tz}
        return None

    out, problems = [], []
    for number, (premise, row) in enumerate(premises.table.iterrows(), start=2):  # row 1 is the header
        label = f"premises.csv row {number}"
        element = row.get("element_id") or None
        if element is None:
            problems.append(f"{premise}: no element_id (every premise on a feeder needs one)")
            continue
        load, imported = source(premise, "load_kw"), source(premise, "import_kw")
        sizes = {c: _number(row, c) for c in ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw")}
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

        def layers(kind, own, label=label):
            return [Layer("built-in", "built-in (v1.1 premises device defaults)", DEVICE_DEFAULTS[kind]),
                    Layer("defaults", f"house_defaults.device_params.{kind}", defaults.device_params.get(kind, {})),
                    Layer("row", label, own)]

        rule_layers = [("defaults", "house_defaults.rules", defaults.rules)]
        devices = []
        pv = source(premise, "pv_kw")
        if pv is not None:
            devices.append(_device("solar", "solar", layers("solar", {"available_kw": pv}), None,
                                   [("house_defaults.counterfactual", default_policies.get("solar"))], rule_layers))
        elif sizes["pv_kw_ac"] is not None:
            problems.append(f"{premise}: pv_kw_ac is set but there's no pv_kw time series for it")
            continue
        if sizes["battery_kwh"] is not None:
            battery = _device("battery", "battery", layers("battery", {
                "capacity_kwh": sizes["battery_kwh"], "p_max_charge_kw": sizes["battery_kw"],
                "p_max_discharge_kw": sizes["battery_kw"]}), None,
                [("house_defaults.counterfactual", default_policies.get("battery"))], rule_layers)
            if "charge_sources" in battery.params and pv is None:  # nothing to charge from
                battery.params.pop("charge_sources")
                battery.sources.pop("charge_sources", None)
            devices.append(battery)
        if sizes["ev_battery_kwh"] is not None:
            connected, driving = source(premise, "ev_connected"), source(premise, "ev_driving_kwh")
            if connected is None or driving is None:
                problems.append(f"{premise}: an EV needs ev_connected and ev_driving_kwh time series")
                continue
            plugged = resolve_array_param(connected, time_grid)
            departures = [t for t in range(1, len(plugged)) if plugged[t - 1] > 0.5 and plugged[t] <= 0.5]
            v2x = str(row.get("ev_v2x") or "0") == "1"
            devices.append(_device("ev", "ev_v2x", layers("ev", {
                "capacity_kwh": sizes["ev_battery_kwh"], "p_max_charge_kw": sizes["ev_charger_kw"],
                "p_max_discharge_kw": sizes["ev_charger_kw"] if v2x else 0.0,
                "connected": [float(x) for x in plugged], "driving_kwh": driving, "departure_period": departures}),
                None, [("house_defaults.counterfactual", default_policies.get("ev"))], rule_layers))

        own = {"external_load": external}
        limits = {k: _number(row, k) for k in ("import_limit_kw", "export_limit_kw")}
        own |= {k: v for k, v in limits.items() if v is not None}
        if row.get("tariff"):
            program = row.get("export_program") or None
            own["billing"] = {
                "tariff": str(Path(data.tariff_dir or DEFAULT_TARIFF_DIR) / f"{row['tariff']}.yaml"),
                "income_tier": data.income_tier,
                "program": ({"type": program} | data.programs.get(program, {})) if program else None,
                "baseline_territory": row.get("baseline_territory") or None, "baseline_code": data.baseline_code,
            }
        settings, sources = _house_layers(config, own, label)
        extra = {"data": "metered" if imported else "gross", **{k: v for k, v in sizes.items() if v is not None},
                 "tariff": row.get("tariff") or "", "export_program": row.get("export_program") or ""}
        out.append(ResolvedPremise(premise, "premises.csv", settings, sources, devices, element_id=element,
                                   has_counterfactual=defaults.counterfactual is not None, extra=extra))
    if problems:
        raise ValueError("premise_data problems:\n" + "\n".join(f"  - {p}" for p in problems))

    elements = None
    if network is not None:
        elements = [
            NetworkElement(element, _number(r, "rating_kw"), _number(r, "overload_cost_per_kwh"), r["parent_id"] or None)
            for element, r in network.table.iterrows()
        ]
    return out, elements


def resolve_feeder(config) -> tuple[list[ResolvedPremise], list | None]:
    """Every premise on a feeder scenario, written-out houses first, and the network
    elements if premise_data names a network file."""
    from_files, elements = _premise_rows(config)
    return [_house(config, house) for house in config.houses] + from_files, elements
