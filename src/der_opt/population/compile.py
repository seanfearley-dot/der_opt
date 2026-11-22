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


class PopulationError(ValueError):
    """Every problem found in a population's files, each a separate line."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("premise_data problems:\n" + "\n".join(f"  - {p}" for p in problems))


def _template_index(folders) -> dict[str, Template]:
    """Templates by name, from every folder given (or the library's devices/ folder)."""
    from der_opt.settings import library_dir

    if not folders:
        folders = [library_dir()[0] / "devices"]
    found: dict[str, Template] = {}
    for folder in folders:
        for path in sorted(Path(folder).glob("*.yaml")):
            try:
                template = load_template(path)
            except ValueError:
                continue  # not a one-device template (a multi-device block, say)
            if template.name in found and found[template.name].path != template.path:
                both = f"template {template.name!r} is in two folders: {found[template.name].path} and {template.path}"
                raise PopulationError([both])
            found[template.name] = template
    return found


class _Series:
    """Finds a premise's (or device's, or weather station's) series across the files."""

    def __init__(self, paths, tz):
        from der_opt.inputs import read_timeseries

        self.paths, self.tz = list(paths), tz
        self.files = [read_timeseries(path) for path in self.paths]

    def find(self, column: str, premise: str, device_id: str | None = None, station: str | None = None) -> dict | None:
        for path, ts in zip(self.paths, self.files, strict=True):
            if ts.stations is not None:
                if station is not None and ts.has(column, station=station):
                    return {"source": "timeseries", "path": path, "weather_station": station, "column": column,
                            "tz": self.tz}
                continue
            if ts.premises is None or not ts.has(column, premise, device_id):
                continue
            ref = {"source": "timeseries", "path": path, "premise_id": premise, "column": column, "tz": self.tz}
            rows = ts.rows_for(premise, device_id, column=column)
            if "device_id" in rows and (rows["device_id"] != "").any():
                ref["device_id"] = rows["device_id"].iloc[0]
            return ref
        return None


SETTING_COLUMNS = ("participation", "baseline_behavior", "response", "envelope_weight", "exclusive_import_export",
                   "poi_limit_kw", "import_limit_kw", "export_limit_kw")


def _v11_devices(premise, row, label, ctx, problems) -> list[ResolvedDevice] | None:
    sizes = {c: _number(row, c) for c in ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw")}
    devices = []
    pv = ctx["series"].find("pv_kw", premise)
    if pv is not None:
        devices.append(_device("solar", "solar", ctx["layers"]("solar", {"available_kw": pv}, label, v11=True), None,
                               ctx["policies"]("solar", "solar"), ctx["rules"]))
    elif sizes["pv_kw_ac"] is not None:
        problems.append(f"{premise}: pv_kw_ac is set but there's no pv_kw time series for it")
        return None
    if sizes["battery_kwh"] is not None:
        battery = _device("battery", "battery", ctx["layers"]("battery", {
            "capacity_kwh": sizes["battery_kwh"], "p_max_charge_kw": sizes["battery_kw"],
            "p_max_discharge_kw": sizes["battery_kw"]}, label, v11=True), None,
            ctx["policies"]("battery", "battery"), ctx["rules"])
        if "charge_sources" in battery.params and pv is None:  # nothing to charge from
            battery.params.pop("charge_sources")
            battery.sources.pop("charge_sources", None)
        devices.append(battery)
    if sizes["ev_battery_kwh"] is not None:
        connected, driving = ctx["series"].find("ev_connected", premise), ctx["series"].find("ev_driving_kwh", premise)
        if connected is None or driving is None:
            problems.append(f"{premise}: an EV needs ev_connected and ev_driving_kwh time series")
            return None
        v2x = str(row.get("ev_v2x") or "0") in ("1", "1.0")
        devices.append(_device("ev", "ev_v2x", ctx["layers"]("ev", {
            "capacity_kwh": sizes["ev_battery_kwh"], "p_max_charge_kw": sizes["ev_charger_kw"],
            "p_max_discharge_kw": sizes["ev_charger_kw"] if v2x else 0.0,
            **ctx["plug_in"](connected, driving)}, label, v11=True), None, ctx["policies"]("ev", "ev"), ctx["rules"]))
    return devices


def _v2_devices(premise, row, ctx, problems) -> list[ResolvedDevice] | None:
    from der_opt.inputs.spec import DEVICE_TYPES

    devices = []
    ok = True
    station = row.get("weather_station") or None
    for _, r in ctx["devices"].for_premise(premise).iterrows():
        device_id, kind = r["device_id"], r["type"]
        label = f"devices.csv row {r['row']}"
        own: dict = {}
        kw, kwh = _number(r, "kw"), _number(r, "kwh")
        for column in r.index:
            if column in ("row", "premise_id", "device_id", "type", "template", "kw", "kwh", "v2x", "installed_cost",
                          "annual_om", "charge_sources"):
                continue
            value = r[column]
            if value is not None and not (isinstance(value, float) and pd.isna(value)) and value != "":
                own[column] = float(value) if isinstance(value, float) else value
        if r.get("charge_sources"):
            own["charge_sources"] = [s.strip() for s in str(r["charge_sources"]).split(";") if s.strip()]
        found = ctx["series"].find
        if kind == "solar":
            pv = found("pv_kw", premise, device_id)
            if pv is not None:
                own["available_kw"] = pv
        elif kind == "battery":
            own |= {k: v for k, v in (("capacity_kwh", kwh), ("p_max_charge_kw", kw), ("p_max_discharge_kw", kw))
                    if v is not None}
        elif kind == "ev":
            own |= {k: v for k, v in (("capacity_kwh", kwh), ("p_max_charge_kw", kw)) if v is not None}
            if kw is not None:
                own["p_max_discharge_kw"] = kw if str(r.get("v2x") or "") in ("1", "1.0") else 0.0
            connected, driving = found("ev_connected", premise, device_id), found("ev_driving_kwh", premise, device_id)
            if connected is not None and driving is not None:
                own |= ctx["plug_in"](connected, driving) | {"pattern": None}  # its series replace a template pattern
        elif kind == "thermostat":
            if kw is not None:
                own["p_max_kw"] = kw
            temp = found("outdoor_temp_c", premise, device_id, station)
            if temp is not None:
                own["ambient_temp_c"] = temp
        elif kind == "flexible_load":
            baseline = found("flexible_kw", premise, device_id)
            if baseline is not None:
                own["baseline_load_kw"] = baseline
        template = None
        if r.get("template"):
            template = ctx["templates"]().get(r["template"])
            if template is None:
                problems.append(f"{premise}/{device_id}: no template {r['template']!r} in "
                                f"{ctx['template_folders'] or 'the library'}")
                ok = False
                continue
        device = _device(device_id, DEVICE_TYPES[kind], ctx["layers"](kind, own, label), template,
                         ctx["policies"](device_id, kind), ctx["rules"])
        if kind == "flexible_load" and kw is not None:  # kw is the most it can shed: spread over its blocks
            blocks = int(device.params.get("max_blocks", 1))
            device.params |= {"max_blocks": blocks, "block_size_kw": kw / blocks}
            device.sources |= {"max_blocks": device.sources.get("max_blocks", label), "block_size_kw": label}
        needs = {"solar": ("available_kw", "pv_kw"), "thermostat": ("ambient_temp_c", "outdoor_temp_c"),
                 "flexible_load": ("baseline_load_kw", "flexible_kw")}
        if kind in needs and needs[kind][0] not in device.params:
            problems.append(f"{premise}/{device_id}: a {kind} needs a {needs[kind][1]} series (or "
                            f"{needs[kind][0]} from its template or defaults)")
            ok = False
        if kind == "ev" and "connected" not in device.params and "pattern" not in device.params:
            problems.append(f"{premise}/{device_id}: an EV needs ev_connected and ev_driving_kwh series, or a "
                            "pattern from its template")
            ok = False
        costs = {c: _number(r, c) for c in ("installed_cost", "annual_om") if _number(r, c) is not None}
        device.costs = costs
        devices.append(device)
    names = {d.device_id for d in devices}
    for device in devices:
        missing = sorted(set(device.params.get("charge_sources", [])) - names)
        if not missing:
            continue
        if device.sources.get("charge_sources", "").startswith("devices.csv"):
            problems.append(f"{premise}/{device.device_id}: charges from {missing}, which aren't devices at {premise}")
            ok = False
        else:  # a default or template written for every premise: keep what this premise has
            kept = [s for s in device.params["charge_sources"] if s in names]
            if kept:
                device.params["charge_sources"] = kept
            else:
                device.params.pop("charge_sources")
                device.sources.pop("charge_sources", None)
    return devices if ok else None


def _premise_rows(config):
    """Premises (and optionally the network and background rows) from input-spec
    files: 2.0 with a devices file, or 1.1 with device columns."""
    from der_opt.inputs import read_network, read_premises
    from der_opt.inputs.spec import read_devices
    from der_opt.network import NetworkElement
    from der_opt.scenarios.data_sources import resolve_array_param
    from der_opt.scenarios.feeder import DEFAULT_TARIFF_DIR
    from der_opt.scenarios.loader import _build_time_grid

    data = config.premise_data
    if data is None:
        return [], None, {}
    defaults = config.house_defaults
    network = read_network(data.network) if data.network else None
    premises = read_premises(data.premises, network)
    devices_file = read_devices(data.devices, premises) if data.devices else None
    series = _Series(data.timeseries, data.tz)
    time_grid = _build_time_grid(config.time_grid)
    default_policies = defaults.counterfactual.policies if defaults.counterfactual is not None else {}
    index: dict = {}

    def templates():
        if "all" not in index:
            index["all"] = _template_index(data.templates)
        return index["all"]

    def layers(kind, own, label, v11=False):
        built_in = DEVICE_DEFAULTS.get(kind, {})
        name = "built-in (v1.1 premises device defaults)" if v11 else "built-in (premises device defaults)"
        return [Layer("built-in", name, built_in),
                Layer("defaults", f"house_defaults.device_params.{kind}", defaults.device_params.get(kind, {})),
                Layer("row", label, own)]

    def policies(device_id, kind):
        policy = default_policies.get(device_id, default_policies.get(kind))
        return [("house_defaults.counterfactual", policy)]

    def plug_in(connected, driving):
        plugged = resolve_array_param(connected, time_grid)
        departures = [t for t in range(1, len(plugged)) if plugged[t - 1] > 0.5 and plugged[t] <= 0.5]
        return {"connected": [float(x) for x in plugged], "driving_kwh": driving, "departure_period": departures}

    ctx = {"series": series, "layers": layers, "policies": policies, "plug_in": plug_in, "devices": devices_file,
           "templates": templates, "template_folders": data.templates,
           "rules": [("defaults", "house_defaults.rules", defaults.rules)]}

    out, background, problems = [], {}, []
    tags = premises.tags
    for number, (premise, row) in enumerate(premises.table.iterrows(), start=2):  # row 1 is the header
        label = f"premises.csv row {number}"
        kind = row.get("kind") or "premise"
        element = row.get("element_id") or None
        if element is None:
            problems.append(f"{premise}: no element_id (every premise on a feeder needs one)")
            continue
        load, imported = series.find("load_kw", premise), series.find("import_kw", premise)
        no_load = (row.get("load") or "") == "none"
        if imported is not None:
            external = imported | {"column": "net_kw"}
        elif load is not None:
            external = load
        elif no_load and kind != "background":
            external = None
        else:
            problems.append(f"{premise}: no load_kw or import_kw time series in {data.timeseries}")
            continue
        if kind == "background":
            background[premise] = (element, external)
            continue
        if devices_file is not None:
            devices = _v2_devices(premise, row, ctx, problems)
            if imported is not None and devices:
                problems.append(f"{premise}: metered data (import_kw/export_kw) already includes its devices, so "
                                "it can't also have devices in the devices file")
                continue
        else:
            sizes = [_number(row, c) for c in ("pv_kw_ac", "battery_kwh", "ev_battery_kwh")]
            if imported is not None and any(v is not None for v in sizes):
                problems.append(f"{premise}: metered data (import_kw/export_kw) already includes its devices, so "
                                "it can't also have solar, battery or EV sizes")
                continue
            devices = _v11_devices(premise, row, label, ctx, problems)
        if devices is None:
            continue

        own = {} if external is None else {"external_load": external}
        for column in SETTING_COLUMNS:
            value = row.get(column)
            if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
                continue
            own[column] = value == "1" if column == "exclusive_import_export" else value
        if row.get("tariff"):
            program = row.get("export_program") or None
            tier = _number(row, "income_tier")
            own["billing"] = {
                "tariff": str(Path(data.tariff_dir or DEFAULT_TARIFF_DIR) / f"{row['tariff']}.yaml"),
                "income_tier": int(tier) if tier is not None else data.income_tier,
                "program": ({"type": program} | data.programs.get(program, {})) if program else None,
                "baseline_territory": row.get("baseline_territory") or None,
                "baseline_code": row.get("baseline_code") or data.baseline_code,
            }
        settings, sources = _house_layers(config, own, label)
        if row.get("customer_class"):
            settings["customer_class"], sources["customer_class"] = row["customer_class"], label
        extra = {"data": "metered" if imported is not None else ("no load" if external is None else "gross")}
        if devices_file is None:
            extra |= {c: _number(row, c) for c in ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh",
                                                   "ev_charger_kw") if _number(row, c) is not None}
        extra |= {"tariff": row.get("tariff") or "", "export_program": row.get("export_program") or ""}
        extra |= {t: row.get(t) for t in tags if row.get(t) not in (None, "")}
        out.append(ResolvedPremise(premise, "premises.csv", settings, sources, devices, element_id=element,
                                   has_counterfactual=defaults.counterfactual is not None
                                   or any(d.counterfactual is not None for d in devices), extra=extra))
    if problems:
        raise PopulationError(problems)

    elements = None
    if network is not None:
        elements = [
            NetworkElement(element, _number(r, "rating_kw"), _number(r, "overload_cost_per_kwh"), r["parent_id"] or None)
            for element, r in network.table.iterrows()
        ]
    return out, elements, background


def resolve_feeder(config) -> tuple[list[ResolvedPremise], list | None, dict]:
    """Every premise on a feeder scenario, written-out houses first; the network elements
    if premise_data names a network file; and background rows from the premises file,
    as name -> (element, load data source)."""
    from_files, elements, background = _premise_rows(config)
    return [_house(config, house) for house in config.houses] + from_files, elements, background
