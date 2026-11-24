"""Answers to a scenario or study file (docs/SCENARIO_CREATOR_SPEC.md v1.0, section 2).

Each part of the file is built as plain data from the answers. A part that is exactly a
library block is written as an `extends` of that block; a device that differs from its
block extends it with the differences beside it; everything else is written out, with
a comment saying what it's for. Paths are written relative to the file. A value the
answers leave out is written as TODO where the file needs one.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from der_opt.creator.answers import Answers

TODO = "TODO"
COMMENTS = {
    "time_grid": "When: the window the run covers",
    "population": "The population's files (docs/INPUT_DATA_SPEC.md)",
    "premises": "Load on the network that isn't a customer",
    "tariffs": "Where premises.csv's `tariff` names are found, and each export program's settings",
    "objective": "What each premise optimizes",
    "devices": "Devices (sizes in kW and kWh)",
    "external_load": "The home's load, kW",
    "value_streams": "What the optimizer pays and earns",
    "poi_limit_kw": "Meter limit, kW, both directions",
    "import_limit_kw": "Most the home can draw, kW",
    "export_limit_kw": "Most the home can send back, kW (0 = non-export)",
    "exclusive_import_export": "A single meter: no importing and exporting in the same step",
    "counterfactual": "Today's behavior (R1): each device's rule",
    "billing": "The bill: tariff and export program",
    "dr": "Demand response (docs/DR_CAPACITY_SPEC.md)",
    "avoided_costs": "First-year screening values",
    "cost_test": "Cost tests (docs/COST_TEST_SPEC.md); `without` is what each device is compared against",
    "defaults": "Applied to every premise in order (a later entry wins)",
    "add_devices": "Devices added to a share of homes",
    "analyses": "What the study runs",
    "weather_case": "The load forecast's weather label, recorded only",
}


@dataclass
class Written:
    path: Path
    text: str
    extends: list[Path] = field(default_factory=list)
    todos: int = 0


# --- building the parts --------------------------------------------------------------


def _abs(path: str | None, base: Path) -> str:
    if not path:
        return TODO
    given = Path(path).expanduser()
    return str(given if given.is_absolute() else (base / given).resolve())


def _or_todo(value):
    return TODO if value is None else value


def _time_grid(a: Answers) -> dict:
    w = a.when
    if w.start:
        return {"resolution_minutes": w.resolution_minutes, "horizon_steps": _or_todo(w.steps), "start": w.start}
    year = w.year or datetime.now().year
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return {"resolution_minutes": 60, "horizon_steps": (366 if leap else 365) * 24, "start": f"{year}-01-01T00:00:00"}


def _series(a: Answers, base: Path, column: str) -> dict:
    return {"source": "timeseries", "path": _abs(a.data.timeseries, base), "premise_id": a.data.premise or TODO,
            "column": column}


def _billing(a: Answers, base: Path) -> dict | None:
    p = a.prices
    if p.objective == "lmp" or (p.objective == "arbitrage" and p.program == "none" and not p.tariff):
        return None
    out = {"tariff": _abs(p.tariff, base), "income_tier": p.income_tier}
    if p.baseline_territory or p.program == "nem2" or _has_baseline(p.tariff, base):
        out["baseline_territory"] = p.baseline_territory or TODO
    if p.program == "nbt":
        out["program"] = {"type": "nbt", "export_rates_csv": _abs(p.export_rates, base),
                          "acc_plus_per_kwh": p.acc_plus_per_kwh}
    elif p.program == "nem2":
        out["program"] = {"type": "nem2", "nsc_rate_per_kwh": p.nsc_rate_per_kwh}
    return out


def _has_baseline(tariff: str | None, base: Path) -> bool:
    path = Path(_abs(tariff, base))
    if not tariff or not path.is_file():
        return False
    from der_opt.billing import load_retail_tariff

    return load_retail_tariff(path).baseline is not None


def _value_streams(a: Answers, base: Path) -> list:
    p = a.prices
    if p.objective == "bill":
        return [{"type": "bill"}]
    if p.objective == "lmp":
        price = _abs(p.price, base)
        return [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "csv", "path": price, "column": "lmp_per_kwh"},
            "export_price": {"type": "csv", "path": price, "column": "lmp_per_kwh"}}}]
    return [{"type": "energy_arbitrage", "params": {
        "import_price": {"type": "retail_tariff", "path": _abs(p.tariff, base)},
        "export_price": {"type": "constant", "price": p.export_price}}}]


def _meter_parts(a: Answers) -> list[dict]:
    m = a.meter
    parts = [{k: v} for k, v in (("poi_limit_kw", m.poi_limit_kw), ("import_limit_kw", m.import_limit_kw),
                                  ("export_limit_kw", m.export_limit_kw)) if v is not None]
    if m.single_meter:
        parts.append({"exclusive_import_export": True})
    return parts


def home_parts(a: Answers, base: Path) -> list[dict]:
    """The home's file as parts (each a mapping of top-level keys), in file order.
    Devices come one part each, so each can match its block."""
    parts: list[dict] = [{"time_grid": _time_grid(a)}]
    for device in a.devices:
        params = dict(device.params)
        if device.type == "thermostat" and "ambient_temp_c" not in params:  # outdoor temperature from the data
            params["ambient_temp_c"] = _series(a, base, "outdoor_temp_c")
        parts.append({"devices": [{"type": device.type, "name": device.name, "params": params}],
                      "_template": device.template})
    if a.solar_from_data and a.prices.objective != "lmp":  # solar from the time series' pv_kw
        parts.append({"devices": [{"type": "solar", "name": "solar",
                                   "params": {"available_kw": _series(a, base, "pv_kw")}}]})
    if a.prices.objective != "lmp":
        parts.append({"external_load": _series(a, base, "load_kw")})
    parts.append({"value_streams": _value_streams(a, base)})
    parts += _meter_parts(a)
    for name, policy in a.behavior.items():
        parts.append({"counterfactual": {"policies": {name: {"type": policy}}}})
    billing = _billing(a, base) if a.prices.billed else None
    if billing is not None:
        parts.append({"billing": billing})
    if a.dr is not None:
        parts.append({"dr": _absolute_paths(a.dr, base)})
    if a.value.avoided_costs and a.value.cost_test is None:
        parts.append({"avoided_costs": {"set": _abs(a.value.avoided_costs, base)}})
    if a.value.cost_test is not None:
        parts.append({"cost_test": _absolute_paths(a.value.cost_test, base)})
    return parts


def study_parts(a: Answers, base: Path) -> list[dict]:
    d = a.data
    population = {"premises": _abs(d.premises, base)}
    if d.devices:
        population["devices"] = _abs(d.devices, base)
    population["timeseries"] = [_abs(f, base) for f in d.timeseries_files] or [TODO]
    if a.feeder and d.network and not a.network.elements:
        population["network"] = _abs(d.network, base)
    population["tz"] = d.tz
    parts: list[dict] = [{"time_grid": _time_grid(a)}, {"population": population}]
    if a.network.background:
        parts.append({"premises": [{"name": b.name, "element": b.element, "kind": "background",
                                    "external_load": _absolute_paths(b.load, base)} for b in a.network.background]})
    p = a.prices
    programs = {"nem2": {"nsc_rate_per_kwh": p.nsc_rate_per_kwh}}
    if p.program == "nbt" or p.export_rates:  # NBT needs its rates; without NBT, nothing to fill in
        programs = {"nbt": {"export_rates_csv": _abs(p.export_rates, base), "acc_plus_per_kwh": p.acc_plus_per_kwh},
                    **programs}
    one = bool(p.tariff) and Path(p.tariff).suffix in (".yaml", ".yml")  # one tariff for every home
    parts.append({"tariffs": {"dir": _abs(str(Path(p.tariff).parent), base) if one else _abs(p.tariff, base),
                              "programs": programs}})
    if one:  # a default: a home whose premises.csv row names a tariff keeps its own
        parts.append({"defaults": [{"set": {"premise": {"billing": _billing(a, base)}}}]})
    # behavior by type and the objective, as one part (the library's study/homes_bill shape)
    behavior = [{"where": {"type": kind}, "set": {"device": {"counterfactual": {"type": policy}}}}
                for kind, policy in a.behavior.items()]
    parts.append({"objective": "bill" if p.objective == "bill" else _value_streams(a, base), "defaults": behavior})
    if a.device_settings:
        parts.append({"defaults": [{"where": {"type": kind}, "set": {"device": dict(settings)}}
                                   for kind, settings in a.device_settings.items()]})
    premise = {}
    m = a.meter
    for key, value in (("poi_limit_kw", m.poi_limit_kw), ("import_limit_kw", m.import_limit_kw),
                       ("export_limit_kw", m.export_limit_kw)):
        if value is not None:
            premise[key] = value
    if m.single_meter:
        premise["exclusive_import_export"] = True
    if a.feeder:
        q = a.participation
        for key, value, default in (("participation", q.participation, "participant"),
                                    ("baseline_behavior", q.baseline_behavior, "own_bill"),
                                    ("response", q.response, "optimized")):
            if value != default:
                premise[key] = value
    if premise:
        parts.append({"defaults": [{"set": {"premise": premise}}]})
    if a.feeder and a.participation.rules:  # mechanism -> {device type: action}, as each type's rules
        by_kind: dict = {}
        for mechanism, actions in a.participation.rules.items():
            for kind, action in (actions or {}).items():
                by_kind.setdefault(kind, {})[mechanism] = action
        parts.append({"defaults": [{"where": {"type": kind}, "set": {"device": {"rules": rules}}}
                                   for kind, rules in by_kind.items()]})
    if a.add_devices:
        parts.append({"add_devices": [entry.model_dump(exclude_defaults=False) for entry in a.add_devices]})
    analyses: dict = {}
    if a.feeder:
        feeder: dict = {}
        if a.network.elements:
            feeder["elements"] = [e.model_dump(exclude_none=True) for e in a.network.elements]
        if a.value.deferral:
            feeder["value_streams"] = [{"type": "capacity_deferral", **{k: _or_todo(v) for k, v in item.items()}}
                                       for item in a.value.deferral]
        coordination = {k: v for k, v in a.coordination.model_dump().items() if v is not None}
        if coordination:
            feeder["coordination"] = _absolute_paths(coordination, base)
        analyses["feeder"] = feeder
    if a.portfolio:
        portfolio: dict = {}
        if a.dr is not None:
            portfolio["dr"] = _absolute_paths(a.dr, base)
        if a.value.avoided_costs:
            portfolio["avoided_costs"] = {"set": _abs(a.value.avoided_costs, base)}
        if a.value.cost_test is not None:
            portfolio["cost_test"] = _absolute_paths(a.value.cost_test, base)
        analyses["portfolio"] = portfolio
    parts.append({"analyses": analyses})
    return parts


def _absolute_paths(data, base: Path):
    """A copy with every value under a path key made absolute from `base`."""
    from der_opt.scenarios.compose import PATH_KEYS

    if isinstance(data, dict):
        return {k: (_abs(v, base) if k in PATH_KEYS and isinstance(v, str) and v != TODO
                    else _absolute_paths(v, base)) for k, v in data.items()}
    if isinstance(data, list):
        return [_absolute_paths(v, base) for v in data]
    return data


# --- matching library blocks ---------------------------------------------------------


def _blocks(library: Path | None) -> list[tuple[Path, dict]]:
    if library is None or not Path(library).is_dir():
        return []
    from der_opt.scenarios.compose import CompositionError, compose_scenario

    out = []
    for path in sorted(Path(library).rglob("*.yaml")):
        try:
            out.append((path, compose_scenario(path).data))
        except CompositionError:
            continue
    return out


def _numbers(data):
    """Numbers by value (a block's 20 is an answer's 20.0)."""
    if isinstance(data, dict):
        return {str(k): _numbers(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_numbers(v) for v in data]
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return float(data)
    return data


def _same(a, b) -> bool:
    return (json.dumps(_numbers(a), sort_keys=True, default=str)
            == json.dumps(_numbers(b), sort_keys=True, default=str))


def _match(part: dict, blocks) -> Path | None:
    return next((path for path, data in blocks if _same(data, part)), None)


def _device_block(template: str | None, blocks) -> tuple[Path, dict] | None:
    if not template:
        return None
    for path, data in blocks:
        if path.stem == template and path.parent.name == "devices" and len(data.get("devices", [])) == 1:
            return path, data["devices"][0]
    return None


def _merge(into: dict, part: dict) -> None:
    from der_opt.scenarios.compose import merge

    for key, value in part.items():
        if key in into and isinstance(into[key], list) and isinstance(value, list):
            into[key] = into[key] + value
        else:
            into[key] = merge(into[key], value, key) if key in into else value


# --- writing -------------------------------------------------------------------------


def _relative_paths(data, folder: Path):
    from der_opt.scenarios.compose import PATH_KEYS

    if isinstance(data, dict):
        return {k: (_path_text(v, folder) if k in PATH_KEYS and isinstance(v, str) and v != TODO
                    else [_path_text(x, folder) if x != TODO else x for x in v]
                    if k in PATH_KEYS and isinstance(v, list) and all(isinstance(x, str) for x in v)
                    else _relative_paths(v, folder)) for k, v in data.items()}
    if isinstance(data, list):
        return [_relative_paths(v, folder) for v in data]
    return data


def _path_text(path: str, folder: Path) -> str:
    path, folder = Path(path).resolve(), Path(folder).resolve()
    common = Path(os.path.commonpath([path, folder]))
    if common in (Path(common.anchor), Path.home(), Path.home().parent):
        return path.as_posix()
    return Path(os.path.relpath(path, folder)).as_posix()


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str)


def _dump(key: str, value) -> str:
    flow = None if isinstance(value, (dict, list)) else False  # a scalar at the top stays `key: value`
    text = yaml.dump({key: value}, Dumper=_Dumper, sort_keys=False, default_flow_style=flow, width=110,
                     allow_unicode=True)
    comment = COMMENTS.get(key)
    return (f"# {comment}\n" if comment else "") + text


def compose_answers(a: Answers, target: Path, base: Path, library: Path | None = None) -> tuple[str, list[Path]]:
    """(the file's text, the blocks it extends)."""
    blocks = _blocks(library)
    parts = study_parts(a, base) if a.study else home_parts(a, base)
    own: dict = {}
    extends: list[Path] = []
    for part in parts:
        template = part.pop("_template", None)
        found = _device_block(template, blocks) if "devices" in part else None
        if found is not None:  # a device from its block, with its changes beside it
            path, block_device = found
            device = part["devices"][0]
            changed = {k: v for k, v in device["params"].items()
                       if not _same(block_device.get("params", {}).get(k), v)}
            extends.append(path)
            if device["name"] != block_device["name"] or device["type"] != block_device["type"]:
                raise ValueError(f"device {device['name']}: the {template} block names its device "
                                 f"{block_device['name']!r}; name it so to use the block")
            if changed:
                _merge(own, {"devices": [{"name": device["name"], "params": changed}]})
            continue
        if template is not None and "devices" in part and not part["devices"][0]["params"]:
            raise ValueError(f"no device block {template!r} in the library")
        match = _match(part, blocks)
        if match is None and "billing" in part and "baseline_territory" in part["billing"]:
            without = copy.deepcopy(part)
            territory = without["billing"].pop("baseline_territory")
            match = _match(without, blocks)
            if match is not None:  # the block, and the customer's territory beside it
                part = {"billing": {"baseline_territory": territory}}
                extends.append(match)
                _merge(own, part)
                continue
        if match is not None:
            extends.append(match)
        else:
            _merge(own, part)
    folder = target.parent
    lines = [(f"# Written by the der-opt scenario creator (docs/SCENARIO_CREATOR_SPEC.md) on "
              f"{datetime.now():%Y-%m-%d}.\n"),
             f"# Check it with:  der-opt validate {target.name}   (relative paths are from this file's folder)\n\n"]
    if extends:
        lines.append("extends:\n" + "".join(f"  - {_path_text(str(p), folder)}\n" for p in extends) + "\n")
    lines.append(f"name: {a.name}\n\n")
    own = _relative_paths(own, folder)
    lines += [_dump(key, value) + "\n" for key, value in own.items()]
    return "".join(lines).rstrip() + "\n", extends


def write(a: Answers, target: Path, base: Path, library: Path | None = None, force: bool = False) -> Written:
    target = Path(target)
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pick another name (nothing was written)")
    text, extends = compose_answers(a, target, base, library)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    written = Written(target, text, extends)
    from der_opt.scenarios.compose import CompositionError, compose_scenario
    from der_opt.scenarios.validate import _placeholders

    try:
        written.todos = len(_placeholders(compose_scenario(target)))
    except CompositionError:
        written.todos = text.count(TODO)
    return written
