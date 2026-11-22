"""`der-opt new`: commented starting scenarios (docs/SCENARIO_COMPOSITION_SPEC.md section 3).

A template is a list of sections. Each is written into the scenario itself or, with a
library folder, the shared ones go into fragment files there and the scenario extends
them. Anything the options don't fill in is written as TODO, which `der-opt validate`
reports until it's replaced. Paths are written relative to the file they're in.
"""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template

import pandas as pd

TODO = "TODO"


@dataclass
class Section:
    text: str  # YAML, with $placeholders; paths are ${path:name}-style keys in `paths`
    fragment: str | None = None  # library file (relative to the library) when shared


@dataclass
class Options:
    timeseries: Path | None = None
    premise: str | None = None
    tariff: Path | None = None
    program: str = "nbt"
    export_rates: Path | None = None
    baseline_territory: str | None = None
    avoided_costs: Path | None = None
    price: Path | None = None
    premises: Path | None = None
    devices: Path | None = None
    network: Path | None = None
    year: int | None = None


@dataclass
class Written:
    scenario: Path
    fragments_written: list[Path] = field(default_factory=list)
    fragments_kept: list[Path] = field(default_factory=list)
    illustrative: list[Path] = field(default_factory=list)  # reused blocks with illustrative values
    todos: int = 0


TEMPLATES = {
    "dr-battery": "one home, full year: a battery in a DR program, with a cost_test section",
    "home-bill": "one home, full year: solar and a battery optimizing the bill (NBT or NEM2)",
    "feeder-from-files": "a study: premises, devices and network from files, with a feeder analysis",
    "lmp-battery": "a standalone battery on a wholesale price",
}


# --- what the options tell us --------------------------------------------------------


def _timeseries_facts(path: Path | None, tz: str = "America/Los_Angeles") -> tuple[list[str], tuple | None]:
    """Premise IDs in a time series file, and (start, resolution, steps) for the period
    it covers on the local clock."""
    if path is None or not path.is_file():
        return [], None
    from der_opt.inputs import read_timeseries

    data = read_timeseries(path, strict=False)
    premises = sorted(data.premises) if data.premises else []
    if data.data.empty or "timestamp" not in data.data:
        return premises, None
    stamps = pd.DatetimeIndex(data.data["timestamp"]).tz_convert(tz).tz_localize(None)
    step = pd.Timedelta(minutes=data.interval_minutes)
    steps = int((stamps.max() + step - stamps.min()) / step)
    return premises, (stamps.min().strftime("%Y-%m-%dT%H:%M:%S"), data.interval_minutes, steps)


def _price_grid(path: Path | None) -> tuple[str, int, int] | None:
    """(start, resolution minutes, steps) covering a price CSV."""
    if path is None or not path.is_file():
        return None
    stamps = pd.to_datetime(pd.read_csv(path, usecols=[0], encoding="utf-8").iloc[:, 0])
    if len(stamps) < 2:
        return None
    minutes = int((stamps.iloc[1] - stamps.iloc[0]).total_seconds() // 60)
    return stamps.iloc[0].strftime("%Y-%m-%dT%H:%M:%S"), minutes, len(stamps)


def _has_baseline(tariff: Path | None) -> bool:
    if tariff is None or not tariff.is_file():
        return False
    from der_opt.billing import load_retail_tariff

    return load_retail_tariff(tariff).baseline is not None


def _root_element(network: Path | None) -> str | None:
    if network is None or not network.is_file():
        return None
    table = pd.read_csv(network, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    roots = table.loc[table.get("parent_id", "") == "", "element_id"]
    return roots.iloc[0] if len(roots) else None


def _year_grid(year: int) -> dict:
    return {"start": f"{year}-01-01T00:00:00", "resolution": 60, "steps": (366 if calendar.isleap(year) else 365) * 24}


# --- the templates -------------------------------------------------------------------
# Fragment names match the blocks in examples/library/, so `--library examples/library`
# reuses those blocks (examples/library/README.md).

TIME = Section("""\
time_grid:
  resolution_minutes: $resolution
  horizon_steps: $steps
  start: "$start"
""", "time/$time_name.yaml")

HOME_BATTERY = Section("""\
devices:
  - type: battery
    name: battery
    params:
      capacity_kwh: 13.5
      p_max_charge_kw: 5.0
      p_max_discharge_kw: 5.0
      eta_charge: 0.95
      eta_discharge: 0.95
      soc_init_frac: 0.5
      soc_final_min_frac: 0.5 # ends where it started, so the year's value is real
      degradation_cost_per_kwh: 0.015 # $$/kWh of throughput, from the warranty
      smoothing_cost_per_kw: 0.10 # $$/kW of change hour to hour: steady output, no cycling through the grid (steers only; never reported)
""", "devices/battery_13kwh.yaml")

HOME_SOLAR_AND_LOAD = Section("""\
devices:
  - type: solar
    name: solar
    params:
      available_kw: {source: timeseries, path: $timeseries, premise_id: $premise, column: pv_kw}

external_load: {source: timeseries, path: $timeseries, premise_id: $premise, column: load_kw}
""")

PANEL_20KW = Section("""\
poi_limit_kw: 20 # both directions
""", "meter/panel_20kw.yaml")

NO_SIMULTANEOUS = Section("""\
# No importing and exporting in the same step: needed whenever export can pay more than
# import (NBT, LMP). Adds a yes/no per step (a MILP).
exclusive_import_export: true
""", "meter/no_simultaneous_import_export.yaml")

SELF_CONSUMPTION = Section("""\
# R1 for a battery: charge from solar surplus, discharge to cover load, no price awareness
counterfactual:
  policies:
    battery: {type: self_consumption}
""", "counterfactual/battery_self_consumption.yaml")

RETAIL_PRICES = Section("""\
# Retail import; a flat export price keeps this a fast LP (an NBT export rate above
# import needs exclusive_import_export, a MILP)
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {type: retail_tariff, path: $tariff}
      export_price: {type: constant, price: 0.05}
""", "value_streams/${tariff_name}_import.yaml")

DR_PROGRAM = Section("""\
# docs/DR_CAPACITY_SPEC.md
dr:
  customer_class: residential # 5-in-10 settlement baseline
  measure_devices: [battery] # R3 = this home without the battery
  program:
    program_id: summer_peak
    season: [5, 6, 7, 8, 9, 10]
    day_types: [weekday]
    window: "16:00-21:00"
    min_hours: 1
    max_hours: 3
    max_events_per_month: 4
    max_consecutive_days: 2
    response: incentive
    incentive_per_kwh: $TODO # $$/kWh of reduction
    capacity_reference: all # R2, R3 and S side by side
  events:
    random: {events_per_month: {6: 2, 7: 3, 8: 3, 9: 2}, seed: 1, hours: 3, min_days_between: 1}
""", "programs/summer_peak_incentive.yaml")

COST_TEST = Section("""\
# docs/COST_TEST_SPEC.md
cost_test:
  scope: control # measure = optimization + DR, against R1
  start_year: $next_year
  measure_life_years: 10
  avoided_costs: {set: $avoided_costs}
  capacity_value: {reference: R2, basis: import, measure: counted}
  net_to_gross: 1.0
  bill_escalation: 0.03
  cost_escalation: 0.025
  participant_costs: {upfront: $TODO, annual_om: 0}
  program_costs: {admin_upfront: 0, admin_annual: 0, upfront_incentive: 0}
  discount_rates: {utility: $TODO, participant: $TODO, societal: $TODO}
""", "cost_tests/$cost_name.yaml")

BILL_OBJECTIVE = Section("""\
# Optimize the bill itself (the billing section): nbt_bill or nem2_bill, by its program.
value_streams:
  - type: bill
""", "value_streams/bill.yaml")

BILLING = Section("""\
billing:
  tariff: $tariff
  income_tier: 3
$baseline_line  program: $program_block
""", "billing/${tariff_name}_${program}.yaml")

FEEDER_DATA = Section("""\
population:
  premises: $premises
$devices_line  timeseries: [$timeseries]
  network: $network
  tz: America/Los_Angeles

tariffs:
  dir: $tariff_dir # premises.csv `tariff` names a file here
  programs: # settings for each export_program premises.csv names
    nbt: {export_rates_csv: $export_rates, acc_plus_per_kwh: 0.0088}
    nem2: {nsc_rate_per_kwh: 0.02781} # check the current rate

defaults:
  - set: {premise: {exclusive_import_export: true}} # no importing and exporting in the same hour

analyses:
  feeder:
    value_streams:
      - {type: capacity_deferral, element: $root_element, value_per_kw: $TODO} # $$/kW of peak avoided
""")

HOMES_BILL = Section("""\
# Every premise optimizes its own bill, against rule-based EVs and self-consuming batteries.
objective: bill
defaults:
  - where: {type: ev}
    set: {device: {counterfactual: {type: rule_based_ev}}}
  - where: {type: battery}
    set: {device: {counterfactual: {type: self_consumption}}}
""", "study/homes_bill.yaml")

DEVICE_PARAMS = Section("""\
# Device behavior for the study's batteries and EVs (sizes come from devices.csv)
defaults:
  - where: {type: battery}
    set: {device: {eta_charge: 0.96, eta_discharge: 0.96, degradation_cost_per_kwh: 0.015, charge_sources: [solar]}}
  - where: {type: ev}
    set: {device: {degradation_cost_per_kwh: 0.0}}
""", "study/device_params.yaml")

GRID_BATTERY = Section("""\
devices:
  - type: battery
    name: battery
    params:
      capacity_kwh: 4000 # 1 MW / 4 MWh
      p_max_charge_kw: 1000
      p_max_discharge_kw: 1000
      eta_charge: 0.94
      eta_discharge: 0.94
      soc_init_frac: 0.5
      soc_final_min_frac: 0.5
      degradation_cost_per_kwh: 0.005
""", "devices/battery_1mw_4mwh.yaml")

LMP_PRICES = Section("""\
# a price CSV from `der-opt fetch-lmp` (timestamp, lmp_per_kwh), both directions
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {type: csv, path: $price, column: lmp_per_kwh}
      export_price: {type: csv, path: $price, column: lmp_per_kwh}
""", "value_streams/${price_name}.yaml")

INTERCONNECTION_1MW = Section("""\
poi_limit_kw: 1000 # no external_load: a standalone battery serves no load
""", "meter/interconnection_1mw.yaml")

SECTIONS = {
    "dr-battery": [TIME, HOME_BATTERY, HOME_SOLAR_AND_LOAD, RETAIL_PRICES, PANEL_20KW, SELF_CONSUMPTION,
                   DR_PROGRAM, COST_TEST],
    "home-bill": [TIME, HOME_BATTERY, HOME_SOLAR_AND_LOAD, BILL_OBJECTIVE, PANEL_20KW, NO_SIMULTANEOUS,
                  SELF_CONSUMPTION, BILLING],
    "feeder-from-files": [TIME, FEEDER_DATA, HOMES_BILL, DEVICE_PARAMS],
    "lmp-battery": [TIME, GRID_BATTERY, LMP_PRICES, INTERCONNECTION_1MW],
}
PATH_VALUES = ("timeseries", "tariff", "export_rates", "avoided_costs", "price", "premises", "devices", "network",
               "tariff_dir")


def _values(template: str, opts: Options) -> tuple[dict[str, str], dict[str, Path | None]]:
    """Plain values and path values (absolute, or None for TODO) for a template."""
    premises_in_file, ts_grid = _timeseries_facts(opts.timeseries)
    grid = _price_grid(opts.price) if template == "lmp-battery" else None if opts.year else ts_grid
    year = opts.year or (int(grid[0][:4]) if grid else datetime.now().year)
    if grid is not None and grid[2] * grid[1] >= 365 * 24 * 60 and grid[0].endswith("-01-01T00:00:00"):
        grid = None  # a whole year: named as one
    if grid is not None:
        start, resolution, steps = grid
        hour = "" if start[11:] == "00:00:00" else f"T{start[11:13]}"
        time_name = f"{start[:10]}{hour}_{steps}x{resolution}min"
    else:
        g = _year_grid(year)
        start, resolution, steps = g["start"], g["resolution"], g["steps"]
        time_name = f"year_{year}"
    premise = opts.premise or (premises_in_file[0] if len(premises_in_file) == 1 else TODO)
    tariff_dir = opts.tariff.parent if opts.tariff else None
    if template == "feeder-from-files":
        from der_opt.scenarios.feeder import DEFAULT_TARIFF_DIR

        tariff_dir = DEFAULT_TARIFF_DIR
    program = opts.program
    if program == "nem2":
        program_block = "{type: nem2, nsc_rate_per_kwh: 0.02781} # check the current rate"
    else:
        program_block = "{type: nbt, export_rates_csv: $export_rates, acc_plus_per_kwh: 0.0088}"
    needs_territory = program == "nem2" or _has_baseline(opts.tariff)
    baseline_line = (f"  baseline_territory: {opts.baseline_territory or TODO} # PG&E baseline territory, e.g. X\n"
                     if needs_territory else "")
    plain = {
        "TODO": TODO, "start": start, "resolution": str(resolution), "steps": str(steps), "time_name": time_name,
        "premise": premise, "next_year": str(year + 1), "program": program,
        "program_block": program_block, "baseline_line": baseline_line,
        "tariff_name": opts.tariff.stem if opts.tariff else "tariff",
        "cost_name": opts.avoided_costs.name if opts.avoided_costs else "cost_test",
        "price_name": opts.price.stem if opts.price else "lmp",
        "root_element": _root_element(opts.network) or TODO,
        "devices_line": ("  devices: $devices\n" if opts.devices else
                         "  # devices: devices.csv (input spec 2.0; `der-opt convert premises` writes one)\n"),
    }
    paths = {"timeseries": opts.timeseries, "tariff": opts.tariff, "export_rates": opts.export_rates,
             "avoided_costs": opts.avoided_costs, "price": opts.price, "premises": opts.premises,
             "devices": opts.devices, "network": opts.network, "tariff_dir": tariff_dir}
    return plain, paths


def _relative(path: Path | None, folder: Path) -> str:
    """`path` as written in a file in `folder`: relative when they share a project
    folder, absolute when they only share the root or home folder."""
    if path is None:
        return TODO
    return _quoted(_path_text(path, folder))


def _path_text(path: Path, folder: Path) -> str:
    path, folder = Path(path).resolve(), Path(folder).resolve()
    common = Path(os.path.commonpath([path, folder]))
    if common in (Path(common.anchor), Path.home(), Path.home().parent):
        return path.as_posix()
    return Path(os.path.relpath(path, folder)).as_posix()


def _quoted(text: str) -> str:
    """A YAML scalar for `text`, quoted when it has characters YAML would misread."""
    import json

    if any(c in text for c in ':#,[]{}&*!|>\'"%@`') or text != text.strip():
        return json.dumps(text)
    return text


def _fill(text: str, plain: dict, paths: dict, folder: Path) -> str:
    values = plain | {k: _relative(v, folder) for k, v in paths.items()}
    once = Template(text).substitute(values)
    return Template(once).safe_substitute(values)  # program_block carries a path of its own


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """A section's top-level keys, each with its lines. A comment at the margin belongs
    to the key below it; indented lines and blank lines to the key above."""
    blocks: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for line in text.rstrip("\n").splitlines():
        if line.startswith("#"):
            pending.append(line)
        elif line and not line[0].isspace():
            blocks.append((line.split(":")[0], [*pending, line]))
            pending = []
        elif pending:
            pending.append(line)
        elif blocks:
            blocks[-1][1].append(line)
    if pending and blocks:
        blocks[-1][1].extend(pending)
    return blocks


def _merge_texts(sections: list[str]) -> str:
    """Sections as one file. A key in several sections (`devices:`, `defaults:`)
    becomes one, with each section's entries under it."""
    order: list[str] = []
    merged: dict[str, list[str]] = {}
    for text in sections:
        for key, lines in _blocks(text):
            while lines and lines[-1] == "":
                lines = lines[:-1]
            if key in merged:
                body = [f"  {line}" if line.startswith("#") else line for line in lines if line.strip() != f"{key}:"]
                merged[key].extend(body)
            else:
                order.append(key)
                merged[key] = list(lines)
    return "\n\n".join("\n".join(merged[key]) for key in order) + "\n"


def _has_illustrative_values(path: Path) -> bool:
    """A value commented `# illustrative` (the starter library marks its placeholders so)."""
    return any("illustrative" in line.split("#", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()
               if "#" in line and not line.lstrip().startswith("#"))


def write_template(template: str, target: Path, opts: Options, library: Path | None = None,
                   force: bool = False) -> Written:
    if template not in SECTIONS:
        raise ValueError(f"no template {template!r}; choose from {sorted(SECTIONS)}")
    target = Path(target)
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass --force to overwrite it")
    plain, paths = _values(template, opts)
    written = Written(target)
    header = (f"# Written by `der-opt new {template}` ({TEMPLATES[template]}).\n"
              f"# Replace every TODO, then check it with:  der-opt validate {target.name}\n"
              "# Relative paths are from this file's folder.\n\n")
    name = target.stem.replace("-", "_")
    own: list[str] = []
    extends: list[str] = []
    for section in SECTIONS[template]:
        if library is not None and section.fragment is not None:
            values = plain
            if section is BILLING and plain["baseline_line"]:
                # the territory is the customer's: it goes in the scenario, not the block
                values = plain | {"baseline_line": ""}
                own.append("billing:\n" + plain["baseline_line"])
            fragment = library / Template(section.fragment).substitute(values)
            if fragment.exists():
                written.fragments_kept.append(fragment)
            else:
                fragment.parent.mkdir(parents=True, exist_ok=True)
                note = f"# A der-opt scenario fragment (`der-opt new {template}`); scenarios use it with `extends`.\n"
                fragment.write_text(note + _fill(section.text, values, paths, fragment.parent), encoding="utf-8")
                written.fragments_written.append(fragment)
            extends.append(_relative(fragment, target.parent))
        else:
            own.append(_fill(section.text, plain, paths, target.parent))
    body = f"name: {name}\n\n"
    if extends:
        body = "extends:\n" + "".join(f"  - {e}\n" for e in extends) + "\n" + body
    text = header + body + _merge_texts(own)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    written.illustrative = [f for f in written.fragments_kept if _has_illustrative_values(f)]
    from der_opt.scenarios.compose import CompositionError, compose_scenario
    from der_opt.scenarios.validate import _placeholders

    try:
        written.todos = len(_placeholders(compose_scenario(target)))
    except CompositionError:
        written.todos = text.count(TODO) - 1
    return written
