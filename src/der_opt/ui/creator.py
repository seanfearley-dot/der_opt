"""The scenario creator's page (docs/SCENARIO_CREATOR_SPEC.md v1.0): thirteen steps on
one page, the answers held as JSON in the browser, every change re-checked on the
server, which shows the file as it would be written and each step's findings.

Fields bind to the answers by `data-k` (a dotted path; a row's keys start with "."). A
key ending `_yaml` holds YAML text the server parses into the key without the suffix.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import yaml

from der_opt.creator.answers import STEPS, Answers, steps_for

# the answers' YAML boxes: (path, where `*` is any list index)
YAML_FIELDS = ("device_settings", "dr.events.schedule", "dr.events.price", "coordination.runs",
               "coordination.price_signal.schedule", "network.background.*.load", "add_devices.*.where",
               "devices.*.more")
SIZE_KEYS = {"battery": ("capacity_kwh", "p_max_charge_kw", "p_max_discharge_kw"),
             "ev_v2x": ("capacity_kwh", "p_max_charge_kw", "p_max_discharge_kw"),
             "thermostat": ("p_max_kw", "comfort_min_c", "comfort_max_c"),
             "curtailable_load": ("block_size_kw", "max_blocks")}
DEFAULTS = {
    "device": {"type": "battery", "name": "battery", "template": "battery_13kwh", "params": {}},
    "element": {"name": "", "rating_kw": None, "parent": None, "overload_cost_per_kwh": None},
    "background": {"name": "background", "element": "", "load_yaml": "source: timeseries\npath: \ncolumn: load_kw\n"},
    "add_devices": {"share": 0.5, "seed": 1, "where_yaml": "", "device": {"device_id": "battery", "type": "battery",
                                                                          "template": "battery_13kwh", "kw": 5,
                                                                          "kwh": 13.5}},
    "deferral": {"element": "", "value_per_kw": None, "threshold_kw": 0},
    "dr": {"customer_class": "residential",
           "program": {"program_id": "summer_peak", "season": [5, 6, 7, 8, 9, 10], "day_types": ["weekday"],
                       "window": "16:00-21:00", "min_hours": 1, "max_hours": 4, "max_events_per_month": 4,
                       "max_consecutive_days": 2, "response": "incentive", "delivery": "uniform",
                       "incentive_per_kwh": None, "settlement_basis": "net", "capacity_reference": "all"},
           "events": {"random": {"events_per_month": {6: 2, 7: 3, 8: 3, 9: 2}, "seed": 1, "hours": 4,
                                 "min_days_between": 1}}},
    "events": {  # each event constructor's starting point (dr.events holds just one)
        "random": {"random": {"events_per_month": {6: 2, 7: 3, 8: 3, 9: 2}, "seed": 1, "hours": 4,
                              "min_days_between": 1}},
        "schedule": {"schedule_yaml": '- {start: "17:00", hours: 4, dates: ["2026-07-15"]}\n'},
        "price": {"price_yaml": "series: {type: retail_tariff, path: }\nevents_per_year: 10\n"},
        "file": {"file": None}},
    "cost_test": {"start_year": None, "measure_life_years": 10, "net_to_gross": 1.0, "bill_escalation": 0.03,
                  "cost_escalation": 0.025, "avoided_costs": {"set": None},
                  "capacity_value": {"reference": "R2", "basis": "import", "measure": "counted"},
                  "participant_costs": {"upfront": None, "annual_om": 0},
                  "program_costs": {"admin_upfront": 0, "admin_annual": 0, "upfront_incentive": 0},
                  "discount_rates": {"utility": None, "participant": None, "societal": None}, "without": {}},
    "price_signal": {"source": "coordinated", "paid": True},
    "envelopes": {"import": {"method": "pro_rata_need"}, "headroom": {"margin_pct": 0}, "granularity": "step",
                  "min_kw": 1.0, "violation_penalty_per_kwh": 5.0},
    "dr_events": {"element": "", "trigger": {"above_pct": 90}, "duration": "variable",
                  "program": {"program_id": "feeder_peak", "season": [6, 7, 8, 9], "window": "16:00-21:00",
                              "min_hours": 1, "max_hours": 4, "response": "incentive", "incentive_per_kwh": None}},
}
MODES = {"battery": ["removed", "rule_based", "backup_only"], "ev_v2x": ["tou_timer", "events_only", "removed", "rule_based"],
         "thermostat": ["rule_based"], "curtailable_load": ["rule_based", "removed"]}
RULES = {"dr_event": {"ev": ["pause", "discharge", "none"], "battery": ["discharge", "none"],
                      "thermostat": ["setback", "none"]},
         "price_signal": {"ev": ["pause", "none"], "battery": ["discharge", "none"], "thermostat": ["setback", "none"]},
         "envelope": {"ev": ["limit", "none"], "battery": ["limit", "none"], "solar": ["curtail", "none"]}}
POLICIES = {"battery": ["self_consumption", "no_flexibility"], "ev_v2x": ["tou_timer", "rule_based_ev", "no_flexibility"],
            "thermostat": ["setpoint", "no_flexibility"], "curtailable_load": ["no_flexibility"]}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


# --- answers in and out of the page ---------------------------------------------------


def _paths(data, pattern: list[str], prefix=()):
    """Concrete paths in `data` matching a dotted pattern with `*` for list indexes."""
    if not pattern:
        yield prefix
        return
    head, rest = pattern[0], pattern[1:]
    if head == "*":
        for i, item in enumerate(data if isinstance(data, list) else []):
            yield from _paths(item, rest, (*prefix, i))
    elif isinstance(data, dict) and (head in data or not rest):
        yield from _paths(data.get(head), rest, (*prefix, head))


def _at(data, path):
    for key in path[:-1]:
        data = data[key]
    return data


def to_form(answers: Answers) -> dict:
    """The answers as the page holds them: YAML boxes as text, sizes and the rest of a
    device's settings apart."""
    data = json.loads(answers.model_dump_json())
    when = data["when"]
    if when.get("start"):  # a window, as the page asks for it
        steps = when.get("steps") or 0
        when.update(mode="window", date=when["start"][:10], time=when["start"][11:16] or "00:00",
                    days=round(steps * when["resolution_minutes"] / 1440, 4) or None)
    else:
        when["mode"] = "year"
    for device in data.get("devices", []):
        sizes = SIZE_KEYS.get(device["type"], ())
        more = {k: v for k, v in device["params"].items() if k not in sizes}
        device["params"] = {k: v for k, v in device["params"].items() if k in sizes}
        device["more"] = more
    for pattern in YAML_FIELDS:
        for path in list(_paths(data, pattern.split("."))):
            holder = _at(data, path) if len(path) > 1 else data
            key = path[-1]
            if isinstance(holder, dict) and key in holder:
                value = holder.pop(key)
                holder[f"{key}_yaml"] = "" if value in (None, {}, []) else yaml.safe_dump(
                    value, sort_keys=False, default_flow_style=None, width=100)
    return data


def from_form(data: dict) -> tuple[Answers, list[tuple[str, str]]]:
    """The page's answers back, with the YAML boxes read; (answers, YAML problems as (path, message))."""
    problems: list[tuple[str, str]] = []

    def walk(node, trail=""):
        if isinstance(node, dict):
            for key in list(node):
                here = f"{trail}.{key}" if trail else key
                if key.endswith("_yaml") and isinstance(node[key], str):
                    text = node.pop(key)
                    try:
                        value = yaml.safe_load(text) if text.strip() else None
                    except yaml.YAMLError as exc:
                        problems.append((here[:-5], f"{here[:-5]} isn't YAML: {str(exc).splitlines()[0]}"))
                        continue
                    if value is not None:  # an empty box keeps the default
                        node[key[:-5]] = value
                else:
                    walk(node[key], here)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{trail}.{i}")

    data = json.loads(json.dumps(data))
    walk(data)
    when = data.setdefault("when", {})
    mode, date, time, days = (when.pop(k, None) for k in ("mode", "date", "time", "days"))
    if mode == "window" and not (date and days):
        problems.append(("when", "a window needs its start date and its length in days"))
    if mode == "window":  # a start and a length, as the time grid needs them
        resolution = int(when.get("resolution_minutes") or 60)
        when.update(year=None, resolution_minutes=resolution, start=f"{date}T{time or '00:00'}:00" if date else None,
                    steps=round(float(days) * 1440 / resolution) if days else None)
    elif mode == "year":
        when.update(start=None, steps=None, resolution_minutes=60)
    for device in data.get("devices", []):
        more = device.pop("more", None) or {}
        device["params"] = {**more, **{k: v for k, v in (device.get("params") or {}).items() if v is not None}}
    for item in data.get("add_devices", []):
        item["where"] = item.get("where") or {}
    participation = data.get("participation") or {}
    rules = {m: {k: dict(r) for k, r in (kinds or {}).items() if isinstance(r, dict) and r.get("action")}
             for m, kinds in (participation.get("rules") or {}).items()}
    for mechanism, kinds in (participation.pop("more_rules", None) or {}).items():  # settings beside the actions
        for kind, settings in (kinds or {}).items():
            rules.setdefault(mechanism, {}).setdefault(kind, {}).update(settings or {})
    if participation:
        participation["rules"] = {m: k for m, k in rules.items() if k}
    cost_test = (data.get("value") or {}).get("cost_test")
    if cost_test:
        cost_test["without"] = {k: v for k, v in (cost_test.get("without") or {}).items() if v}
    dr = data.get("dr")
    for program in (dr, ((data.get("coordination") or {}).get("dr_events"))):  # months as numbers (JSON made them text)
        random = ((program or {}).get("events") or {}).get("random")
        if isinstance(random, dict) and isinstance(random.get("events_per_month"), dict):
            random["events_per_month"] = {int(k) if str(k).isdigit() else k: v
                                          for k, v in random["events_per_month"].items()}
    if dr and isinstance(dr.get("events"), dict):  # one constructor: the one picked
        for kind in list(dr["events"]):
            if dr["events"][kind] in (None, {}):
                dr["events"].pop(kind)
    for key in ("dr", "value"):
        if data.get(key) is None:
            data.pop(key, None)
    return Answers.model_validate(data), problems


YAML_STEPS = {"when": 2, "devices": 5, "device_settings": 5, "add_devices": 5, "dr": 10, "coordination": 11, "value": 12,
              "network": 4, "participation": 9}


# --- the page ------------------------------------------------------------------------


def _f(label: str, key: str, kind: str = "text", hint: str = "", options=None, attrs: str = "") -> str:
    """A field: text, num, bool, list (comma-separated), yaml (a box), or select."""
    if kind == "select":
        control = (f'<select data-k="{key}" {attrs}>'
                   + "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in options) + "</select>")
    elif kind == "date":
        control = f'<input type="date" data-k="{key}" {attrs}>'
    elif kind == "bool":
        control = f'<input type="checkbox" data-k="{key}" data-t="bool" {attrs}>'
    elif kind == "yaml":
        control = f'<textarea data-k="{key}_yaml" rows="4" spellcheck="false" {attrs}></textarea>'
    elif 'list="dl-' in attrs:  # a file: a dropdown of the project's, or another path typed in
        files = attrs.split('list="dl-')[1].split('"')[0]
        add = ' data-add="1"' if kind == "list" else ""
        typed = ' data-t="list"' if kind == "list" else ""
        control = (f'<span class="pickpath"><select data-pick="{key}" data-files="{files}"{add}></select>'
                   f'<input data-k="{key}"{typed} autocomplete="off" placeholder="a path in the project" hidden>'
                   "</span>")
    else:
        t = {"num": ' data-t="num" inputmode="decimal"', "list": ' data-t="list"', "pairs": ' data-t="pairs"'}.get(
            kind, "")
        control = f'<input data-k="{key}"{t} autocomplete="off" {attrs}>'
    return (f'<label class="f"><span class="lab">{esc(label)}</span>{control}'
            + (f'<span class="note">{esc(hint)}</span>' if hint else "") + "</label>")


def _rows(name: str, path: str, row: str, add: str) -> str:
    return (f'<div class="rows" data-list="{path}" data-new="{esc(json.dumps(DEFAULTS[name]))}">'
            f'<template>{row}<button type="button" class="small" data-remove>Remove</button></template>'
            f'<div class="items"></div><button type="button" class="small" data-add>{esc(add)}</button></div>')


def _step(number: int, body: str) -> str:
    _, title = STEPS[number]
    return f'<section class="step" data-step="{number}" hidden><h2>{number}. {esc(title)}</h2>{body}' \
           f'<div class="step-checks" data-checks="{number}"></div></section>'


def steps_html(device_blocks: list[str], files: dict[str, list[str]]) -> str:
    types = [("battery", "battery"), ("ev_v2x", "EV"), ("thermostat", "thermostat"),
             ("curtailable_load", "curtailable load")]
    template_options = [("", "none: every setting written out")] + [(b, b) for b in device_blocks]
    device_row = (
        '<div class="row-grid">'
        + _f("Type", ".type", "select", options=types, attrs="data-rerender")
        + _f("Name", ".name", hint="battery, ev, thermostat: a block's own name")
        + _f("From block", ".template", "select", options=template_options, hint="its settings, yours beside them")
        + '<div data-types="battery">' + _f("kWh", ".params.capacity_kwh", "num") + _f("Charge kW", ".params.p_max_charge_kw", "num")
        + _f("Discharge kW", ".params.p_max_discharge_kw", "num") + "</div>"
        + '<div data-types="ev_v2x">' + _f("Battery kWh", ".params.capacity_kwh", "num")
        + _f("Charger kW", ".params.p_max_charge_kw", "num")
        + _f("V2X kW", ".params.p_max_discharge_kw", "num", "0 = charge only") + "</div>"
        + '<div data-types="thermostat">' + _f("HVAC kW (electric)", ".params.p_max_kw", "num")
        + _f("Comfort low, °C", ".params.comfort_min_c", "num") + _f("Comfort high, °C", ".params.comfort_max_c", "num")
        + '<p class="note">Outdoor temperature comes from the time series\' outdoor_temp_c column.</p></div>'
        + _f("Other settings", ".more", "yaml", "YAML: eta_charge, degradation_cost_per_kwh, pattern, ...")
        + "</div>")
    element_row = ('<div class="row-grid">' + _f("Name", ".name") + _f("Parent", ".parent", hint="upstream; blank = root")
                   + _f("Normal rating, kW", ".rating_kw", "num")
                   + _f("Overload $/kWh", ".overload_cost_per_kwh", "num", "blank = hard limit at normal")
                   + _f("Emergency rating, kW", ".emergency_rating_kw", "num", "optional")
                   + _f("Violation $/kWh", ".violation_cost_per_kwh", "num", "above emergency; blank = hard wall")
                   + _f("Emergency kWh/day", ".emergency_kwh_per_day", "num", "optional budget above normal") + "</div>")
    background_row = ('<div class="row-grid">' + _f("Name", ".name") + _f("Element", ".element")
                      + _f("Load", ".load", "yaml", "a data source: timeseries (path, column) or loadcast (path, "
                                                     "trace: poe10, var: net)") + "</div>")
    add_row = ('<div class="row-grid">' + _f("Device id", ".device.device_id") + _f("Type", ".device.type", "select",
               options=[("battery", "battery"), ("ev", "EV"), ("solar", "solar"), ("thermostat", "thermostat")])
               + _f("Template", ".device.template") + _f("kW", ".device.kw", "num") + _f("kWh", ".device.kwh", "num")
               + _f("Share of homes", ".share", "num", "0-1") + _f("Seed", ".seed", "num")
               + _f("Which homes", ".where", "yaml", "a where, e.g. {element_id: tx_1}; blank = all") + "</div>")
    deferral_row = ('<div class="row-grid">' + _f("Element", ".element") + _f("$/kW (a year)", ".value_per_kw", "num")
                    + _f("Above kW", ".threshold_kw", "num") + "</div>")
    files_hint = "in the project; " + (f"e.g. {files['csv'][0]}" if files.get("csv") else "a CSV")
    s = []
    s.append(_step(1, _f("What to build", "kind", "select", options=[
        ("home", "a single home"), ("portfolio", "a study: several homes, each on its own (portfolio)"),
        ("feeder", "a study: homes on a network (feeder)"), ("both", "a study: feeder and portfolio")],
        attrs="data-rerender") + _f("Name", "name", hint="letters, digits and _")))
    hours = [(f"{h:02d}:00", f"{h:02d}:00") for h in range(24)]
    s.append(_step(2, _f("Period", "when.mode", "select", options=[
        ("year", "a calendar year, hourly"), ("window", "a window: a start and a length")])
        + '<div data-when="when.mode=year">'
        + _f("Year", "when.year", "num", "cost tests need a whole calendar year") + "</div>"
        + '<div data-when="when.mode=window">' + _f("Starts", "when.date", "date")
        + _f("At", "when.time", "select", options=hours, attrs='data-default="00:00"')
        + _f("Length, days", "when.days", "num", "e.g. 7, or 3 for a peak's three days")
        + _f("Step", "when.resolution_minutes", "select", attrs='data-default="60"',
             options=[("60", "1 hour"), ("30", "30 minutes"), ("15", "15 minutes")]) + "</div>"))
    s.append(_step(3, '<div data-kinds="home">' + _f("Time series file", "data.timeseries", hint=files_hint,
                                                       attrs='list="dl-csv"')
             + _f("Premise", "data.premise", hint="its premise_id in that file") + "</div>"
             + '<div data-kinds="portfolio feeder both">' + _f("Premises file", "data.premises", attrs='list="dl-csv"')
             + _f("Devices file", "data.devices", attrs='list="dl-csv"')
             + _f("Time series files", "data.timeseries_files", "list", "comma-separated", attrs='list="dl-csv"')
             + '<div data-kinds="feeder both">' + _f("Network file", "data.network", hint="or enter elements in step 4",
                                                     attrs='list="dl-csv"') + "</div>"
             + _f("Time zone", "data.tz") + "</div>"))
    s.append(_step(4, '<p class="note">Rows here replace the network file. The congestion price on an element is the '
                      'cost of one more kW through it: a hard limit gives exact prices but stops the run if load nobody '
                      'can move exceeds it; a violation cost keeps it solvable and flags those hours.</p>'
                   + _rows("element", "network.elements", element_row, "Add an element")
                   + "<h3>Load that isn't a customer</h3>" + _rows("background", "network.background", background_row,
                                                                   "Add background load")))
    s.append(_step(5, '<div data-kinds="home">' + _f("Solar from the data", "solar_from_data", "bool",
                                                       "the time series' pv_kw")
                   + _rows("device", "devices", device_row, "Add a device") + "</div>"
                   + '<div data-kinds="portfolio feeder both"><p class="note">The devices file sets who has what; '
                     'here, each type\'s settings, and devices added to a share of homes.</p>'
                   + _f("Settings by type", "device_settings", "yaml",
                        "battery: {eta_charge: 0.96, charge_sources: [solar]}")
                   + _rows("add_devices", "add_devices", add_row, "Add devices to a share of homes") + "</div>"))
    s.append(_step(6, _f("Optimize", "prices.objective", "select", options=[
        ("bill", "the bill (tariff and export program)"), ("arbitrage", "an import price (the tariff) and a flat export"),
        ("lmp", "a wholesale price file, both ways")], attrs="data-rerender")
        + '<div data-when="prices.objective=bill|arbitrage">'
        + _f("Tariff", "prices.tariff", hint="a tariff YAML (a study: the tariffs folder)", attrs='list="dl-tariffs"')
        + '<div data-kinds="home" data-when="prices.objective=arbitrage">'
        + _f("Flat export $/kWh", "prices.export_price", "num", "what the optimizer is paid for exports")
        + _f("Bill it too", "prices.billed", "bool", "a billing section, so the report shows the bill") + "</div>"
        + '<div data-when="prices.objective=bill||prices.billed=true">'
        + _f("Export program", "prices.program", "select", attrs="data-rerender",
             options=[("nbt", "NBT export credits"), ("nem2", "NEM 2.0"), ("none", "none: exports earn nothing")])
        + '<div data-when="prices.program=nbt">' + _f("NBT export rates", "prices.export_rates", hint="CSV",
                                                         attrs='list="dl-csv"') + "</div>"
        + _f("Baseline territory", "prices.baseline_territory", hint="PG&E, for a tariff with a baseline (e.g. X)")
        + "</div></div>"
        + '<div data-when="prices.objective=lmp">' + _f("Price file", "prices.price", hint="timestamp, lmp_per_kwh",
                                                       attrs='list="dl-csv"') + "</div>"))
    s.append(_step(7, _f("Meter limit, kW", "meter.poi_limit_kw", "num", "both directions")
             + _f("Import limit, kW", "meter.import_limit_kw", "num") + _f("Export limit, kW", "meter.export_limit_kw",
                                                                            "num", "0 = non-export")
             + _f("Single meter", "meter.single_meter", "bool", "no importing and exporting at once; needed when "
                                                                 "export can pay more than import")))
    s.append(_step(8, '<p class="note">How each device runs today, the rule-based counterfactual.</p>'
                   '<div class="behavior"></div>'))
    s.append(_step(9, _f("Participation", "participation.participation", "select",
                         options=[("participant", "participant"), ("non_participant", "non-participant")])
                   + _f("Baseline behavior", "participation.baseline_behavior", "select",
                        options=[("own_bill", "optimizes its own bill"), ("rule_based", "rule-based")])
                   + _f("Response", "participation.response", "select", attrs="data-rerender",
                        options=[("optimized", "optimized"), ("rules", "by rules")])
                   + '<div data-when="participation.response=rules"><h3>Rules</h3><p class="note">What each device '
                     'type does in each mechanism; blank keeps der-opt\'s default (an EV pauses for an event, a '
                     'battery discharges to the home, a thermostat sets back 2 °C).</p><div class="rules"></div>'
                   + _f("More rule settings", "participation.more_rules", "yaml",
                        "dr_event: {ev: {catch_up: true}, battery: {to: export}}") + "</div>"
                   + '<p class="note">Study-wide defaults; a premises column of the same name wins, and single '
                     'premises change in the Study settings tab afterwards.</p>'))
    s.append(_step(10, '<label class="f"><span class="lab">A DR program</span><input type="checkbox" data-enable="dr">'
                       "</label>"
                   + '<div data-needs="dr">' + _f("Program id", "dr.program.program_id")
                   + _f("Months", "dr.program.season", "list", "comma-separated")
                   + _f("Window", "dr.program.window", hint="local, 16:00-21:00")
                   + _f("Shortest event, h", "dr.program.min_hours", "num") + _f("Longest event, h", "dr.program.max_hours",
                                                                               "num")
                   + _f("Events a month, at most", "dr.program.max_events_per_month", "num")
                   + _f("Response", "dr.program.response", "select", attrs="data-rerender", options=[
                       ("incentive", "incentive"), ("commitment", "commitment"), ("import_cap", "import cap")])
                   + '<div data-when="dr.program.response=incentive">'
                   + _f("Incentive $/kWh", "dr.program.incentive_per_kwh", "num")
                   + _f("Delivery", "dr.program.delivery", "select", attrs='data-default="per_kwh"', options=[
                       ("uniform", "uniform: the kW held every event hour"), ("per_kwh", "per kWh")]) + "</div>"
                   + '<div data-when="dr.program.response=commitment">'
                   + _f("Committed kW", "dr.program.committed_kw", "num", "below the baseline, every event hour")
                   + "</div>"
                   + '<div data-when="dr.program.response=import_cap">'
                   + _f("Import cap kW", "dr.program.cap_kw", "num") + "</div>"
                   + _f("Settled on", "dr.program.settlement_basis", "select", attrs='data-default="net"', options=[
                       ("net", "net load (exports count)"), ("import", "grid import only")])
                   + '<label class="f"><span class="lab">Events</span><select data-events>'
                     '<option value="random">random, by month</option><option value="schedule">a schedule</option>'
                     '<option value="price">on the highest prices</option><option value="file">from a file</option>'
                     "</select></label>"
                   + '<div data-when="dr.events.random">'
                   + _f("Events by month", "dr.events.random.events_per_month", "pairs", "month: count, e.g. 6: 2, 7: 3")
                   + _f("Hours each", "dr.events.random.hours", "num") + _f("Seed", "dr.events.random.seed", "num")
                   + _f("Days between, at least", "dr.events.random.min_days_between", "num") + "</div>"
                   + '<div data-when="?dr.events.schedule_yaml">' + _f("Schedule", "dr.events.schedule", "yaml",
                     "rules: {start, hours, dates | days and months | nth_weekday}") + "</div>"
                   + '<div data-when="?dr.events.price_yaml">' + _f("Price-based", "dr.events.price", "yaml",
                     "series, events_per_year, duration") + "</div>"
                   + '<div data-when="?dr.events.file">' + _f("Events file", "dr.events.file",
                     hint="event_id, start, end, program_id, event_type", attrs='list="dl-csv"') + "</div>"
                   + '<div data-kinds="home">' + _f("Customer class", "dr.customer_class", "select", options=[
                       ("residential", "residential (5-in-10)"), ("commercial", "commercial (10-in-10)")]) + "</div>"
                   + "</div>"))
    mech = "".join(
        f'<label class="f"><span class="lab">{esc(t)}</span><input type="checkbox" data-enable="coordination.{k}" '
        f'data-default-key="{k}"></label><div data-needs="coordination.{k}">{body}</div>'
        for k, t, body in (
            ("price_signal", "A price signal", _f("Source", "coordination.price_signal.source", "select", options=[
                ("coordinated", "the coordinated run's congestion prices"), ("schedule", "a schedule"),
                ("file", "a file")], attrs="data-rerender") + _f("Paid", "coordination.price_signal.paid", "bool")
             + '<div data-when="coordination.price_signal.source=schedule">'
             + _f("Schedule", "coordination.price_signal.schedule", "yaml", "[{element, per_kwh, hours}]") + "</div>"
             + '<div data-when="coordination.price_signal.source=file">'
             + _f("Price file", "coordination.price_signal.path", hint="timestamp, element_id, price_per_kwh",
                  attrs='list="dl-csv"') + "</div>"),
            ("envelopes", "Operating envelopes", _f("Import shared by", "coordination.envelopes.import.method", "select",
                                                    options=[(m, m) for m in ("pro_rata_need", "equal", "service_size",
                                                                              "weights", "max_min", "fixed", "file")])
             + _f("Margin %", "coordination.envelopes.headroom.margin_pct", "num")
             + _f("Granularity", "coordination.envelopes.granularity", hint="step, static")
             + _f("Minimum kW", "coordination.envelopes.min_kw", "num")
             + _f("Violation $/kWh", "coordination.envelopes.violation_penalty_per_kwh", "num")),
            ("dr_events", "Feeder DR events", _f("Element", "coordination.dr_events.element")
             + _f("Trigger above % of rating", "coordination.dr_events.trigger.above_pct", "num")
             + _f("Incentive $/kWh", "coordination.dr_events.program.incentive_per_kwh", "num")
             + _f("Longest event, h", "coordination.dr_events.program.max_hours", "num"))))
    s.append(_step(11, mech + _f("Runs", "coordination.runs", "yaml",
                                 "blank: each mechanism alone and all together; or {name: [envelopes, ...]}")))
    s.append(_step(12, _f("Avoided cost set", "value.avoided_costs", hint="a folder with manifest.yaml (screening)",
                          attrs='list="dl-sets"')
                   + '<div data-kinds="home portfolio both"><label class="f"><span class="lab">Cost tests</span>'
                     '<input type="checkbox" data-enable="value.cost_test" data-default-key="cost_test"></label>'
                   + '<div data-needs="value.cost_test">' + _f("Start year", "value.cost_test.start_year", "num")
                   + _f("Life, years", "value.cost_test.measure_life_years", "num")
                   + _f("Avoided cost set", "value.cost_test.avoided_costs.set", attrs='list="dl-sets"')
                   + _f("Participant upfront $", "value.cost_test.participant_costs.upfront", "num",
                        "include the installed cost of any device compared as removed")
                   + _f("Participant $/year", "value.cost_test.participant_costs.annual_om", "num")
                   + _f("Program admin upfront $", "value.cost_test.program_costs.admin_upfront", "num")
                   + _f("Program admin $/year", "value.cost_test.program_costs.admin_annual", "num")
                   + _f("Discount rate, utility", "value.cost_test.discount_rates.utility", "num")
                   + _f("Discount rate, participant", "value.cost_test.discount_rates.participant", "num")
                   + _f("Discount rate, societal", "value.cost_test.discount_rates.societal", "num")
                   + '<h3>What each device is compared against</h3><div class="without"></div>'
                     '<p class="note">removed: the home without it (its cost belongs in the participant cost); '
                     'rule_based: its ordinary rule; tou_timer: an EV charging from the cheapest TOU period; '
                     'events_only: that EV, responding to events; backup_only: a battery held full, discharging only '
                     'into events. A battery has no default.</p>'
                   + "</div></div>"
                   + '<div data-kinds="feeder both"><h3>The network\'s own value</h3>'
                   + _rows("deferral", "value.deferral", deferral_row, "Add capacity deferral") + "</div>"))
    s.append(_step(13, _f("File to write", "_target", hint="in the project; never overwritten")
                   + '<div class="row"><button type="button" data-write>Write it</button> '
                     '<button type="button" data-write="run">Write and run</button></div>'
                     '<div id="write-result"></div><p class="note">Every check, by step:</p>'
                     '<div class="all-checks"></div>'))
    return "".join(s)


def page(project, token: str, library: Path, files: dict[str, list[str]]) -> str:
    from der_opt.creator.presets import PRESETS
    from der_opt.ui.views import layout

    device_blocks = sorted(p.stem for p in (library / "devices").glob("*.yaml")) if library else []
    from der_opt.scenarios.compose import CompositionError, compose_scenario

    block_params = {}  # each device block's settings, shown as placeholders
    for stem in device_blocks:
        try:
            devices = compose_scenario(library / "devices" / f"{stem}.yaml").data.get("devices") or [{}]
            block_params[stem] = devices[0].get("params", {})
        except CompositionError:
            continue
    nav = "".join(f'<a href="#" data-goto="{n}"><span class="n">{n}</span> {esc(t)}<span class="badge"></span></a>'
                  for n, (_, t) in STEPS.items())
    presets = "".join(f'<button type="button" class="small" data-preset="{esc(k)}" title="{esc(v)}">{esc(k)}</button>'
                      for k, v in PRESETS.items())
    lists = "".join(f'<datalist id="dl-{k}">' + "".join(f'<option value="{esc(v)}">' for v in values) + "</datalist>"
                    for k, values in files.items())
    start = to_form(Answers())
    start["_target"] = "scenarios/new_scenario.yaml"

    def embedded(value) -> str:
        return json.dumps(value).replace("</", "<\\/")

    start_json, defaults_json = embedded(start), embedded(DEFAULTS)
    tariff_dirs = sorted({str(Path(t).parent) for t in files.get("tariffs", [])})
    extra_json = embedded({"blocks": block_params, "modes": MODES, "rules": RULES,
                           "files": {**files, "tariff_dirs": tariff_dirs}})
    body = f"""<div class="crumbs"><a href="/">Scenarios and runs</a> / new</div><h1>New scenario or study</h1>
<p class="note">Start from a preset, or step by step. The file on the right is what will be written; each step lists
its checks as you go. Nothing is written until Review.</p><div class="presets">Presets: {presets}</div>{lists}
<div class="creator"><nav class="steps">{nav}</nav><form class="flow" onsubmit="return false">
{steps_html(device_blocks, files)}<div class="row"><button type="button" class="small" data-prev>Back</button>
<button type="button" class="small" data-next>Next</button></div></form>
<aside class="preview"><div class="summary"></div><pre class="yaml"></pre></aside></div>
<script type="application/json" id="creator-start">{start_json}</script>
<script type="application/json" id="creator-defaults">{defaults_json}</script>
<script type="application/json" id="creator-policies">{json.dumps(POLICIES)}</script>
<script type="application/json" id="creator-extra">{extra_json}</script>
<script type="application/json" id="creator-kinds">{json.dumps({k: steps_for(k) for k in
                                                                  ("home", "portfolio", "feeder", "both")})}</script>"""
    return layout(project, "New", body, token, "new", extra=f"<style>{CSS}</style><script>{JS}</script>")


def preview(project, form: dict, library: Path | None) -> dict:
    """{text, items: [{step, level, message}]} for the answers as they stand."""
    from der_opt.creator.check import Item, check

    try:
        answers, problems = from_form(json.loads(form["answers"]))
    except Exception as exc:  # noqa: BLE001 -- shown on the page as it is
        return {"text": "", "items": [{"step": 1, "level": "error", "message": f"the answers don't read: {exc}"}]}
    target = project.inside(form.get("target") or "scenarios/new_scenario.yaml")
    text, items = check(answers, target, project.root, library)
    items = [Item(YAML_STEPS.get(path.split(".")[0], 13), "error", message) for path, message in problems] + items
    return {"text": text, "items": [i.__dict__ for i in items]}


def preset_form(name: str) -> dict:
    from der_opt.creator.presets import preset
    from der_opt.scenarios.templates import Options

    data = to_form(preset(name, Options(), scenario_name=name.replace("-", "_")))
    data["_target"] = f"scenarios/{name.replace('-', '_')}.yaml"
    return data


def write_answers(project, form: dict, library: Path | None) -> Path:
    from der_opt.creator.writer import write

    answers, problems = from_form(json.loads(form["answers"]))
    if problems:
        raise ValueError("; ".join(m for _, m in problems))
    relative = (form.get("target") or "").strip()
    if not relative.endswith((".yaml", ".yml")):
        raise ValueError("the file to write ends in .yaml")
    target = project.inside(relative)
    write(answers, target, project.root, library)
    return target


CSS = """
.presets{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:8px 0 14px}
.pickpath{display:flex;flex-direction:column;gap:4px;min-width:0}.pickpath select,.pickpath input{width:100%}
.creator{display:grid;grid-template-columns:200px minmax(0,1fr) minmax(0,440px);gap:18px;align-items:start}
nav.steps{position:sticky;top:12px;display:flex;flex-direction:column;gap:2px}
nav.steps a{color:var(--muted);text-decoration:none;padding:5px 8px;border-radius:6px;display:flex;gap:6px;align-items:center}
nav.steps a.on{background:var(--chip,#eef1f6);color:var(--ink)} nav.steps a .n{width:18px;text-align:right}
nav.steps a .badge{margin-left:auto;font-size:11px;border-radius:9px;padding:0 6px}
nav.steps a .badge.err{background:#b91c1c;color:#fff} nav.steps a .badge.warn{background:#b45309;color:#fff}
form.flow{min-width:0} .step h2{margin-top:0}
label.f{display:grid;grid-template-columns:190px minmax(0,1fr);gap:4px 10px;align-items:center;margin:6px 0}
label.f .note{grid-column:2;font-size:12px} label.f input,label.f select,label.f textarea{width:100%}
label.f textarea{font-family:ui-monospace,Menlo,monospace;font-size:12px}
label.f input[type=checkbox]{width:auto;justify-self:start}
.rows .item{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}
.row-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:2px 12px}
.row-grid>div{display:contents}
.row-grid label.f{grid-template-columns:1fr;gap:2px;margin:2px 0;align-content:start}
.row-grid label.f .lab{font-size:12px;color:var(--muted)} .row-grid label.f .note{grid-column:1;font-size:11px}
.row-grid label.f:has(textarea){grid-column:1/-1}
.rules-grid{display:grid;grid-template-columns:110px repeat(3,minmax(0,1fr));gap:4px 10px;align-items:center;
margin:6px 0 10px}.rules-grid .head{color:var(--muted);font-size:12px}
.preview{position:sticky;top:12px;max-height:calc(100vh - 40px);overflow:auto}
.preview pre.yaml{font-size:11.5px;line-height:1.35;white-space:pre;overflow:auto;border:1px solid var(--line);
border-radius:8px;padding:10px;margin:6px 0 0}
.step-checks ul,.all-checks ul{margin:6px 0;padding-left:18px} .lv-error{color:#b91c1c} .lv-warning{color:#b45309}
@media (max-width:1100px){.creator{grid-template-columns:1fr}nav.steps{position:static;flex-direction:row;flex-wrap:wrap}}
"""

JS = r"""
(function(){
const START=JSON.parse(document.getElementById("creator-start").textContent);
const DEF=JSON.parse(document.getElementById("creator-defaults").textContent);
const POL=JSON.parse(document.getElementById("creator-policies").textContent);
const KINDS=JSON.parse(document.getElementById("creator-kinds").textContent);
const X=JSON.parse(document.getElementById("creator-extra").textContent);
let S=START,step=1,timer=null,seq=0;
const form=document.querySelector("form.flow");
const parts=p=>p.split(".").map(x=>/^\d+$/.test(x)?+x:x);
function get(p){let o=S;for(const k of parts(p)){if(o==null)return undefined;o=o[k];}return o;}
function set(p,v){const ks=parts(p);let o=S;ks.slice(0,-1).forEach((k,i)=>{if(o[k]==null||typeof o[k]!=="object")
o[k]=typeof ks[i+1]==="number"?[]:{};o=o[k];});o[ks[ks.length-1]]=v;}
function value(el){const t=el.dataset.t;if(t==="bool")return el.checked;if(t==="num")return el.value.trim()===""?null:
(isNaN(+el.value)?el.value:+el.value);if(t==="list")return el.value.split(",").map(x=>x.trim()).filter(Boolean)
.map(x=>isNaN(+x)?x:+x);if(t==="pairs"){const o={};el.value.split(",").forEach(p=>{const[k,v]=p.split(":").map(x=>
x&&x.trim());if(k&&v!==undefined&&v!=="")o[k]=isNaN(+v)?v:+v;});return o;}return el.value===""?null:el.value;}
function show(el,v){if(el.dataset.t==="bool"){el.checked=!!v;return;}if(el.dataset.t==="pairs"){
el.value=v&&typeof v==="object"?Object.entries(v).map(([k,x])=>k+": "+x).join(", "):"";return;}
if(v==null&&el.dataset.default){el.value=el.dataset.default;return;}
el.value=v==null?"":Array.isArray(v)?v.join(", "):v;}
function holds(cond){return cond.split("||").some(c=>{if(c.startsWith("?")){const ks=parts(c.slice(1));
let o=S;for(const k of ks.slice(0,-1)){if(o==null)return false;o=o[k];}return o!=null&&ks[ks.length-1] in o;}
const[p,vals]=c.split("=");const v=get(p);if(vals===undefined)return v!=null;return vals.split("|").includes(String(v));});}
function rows(){form.querySelectorAll("[data-list]").forEach(box=>{const path=box.dataset.list,list=get(path)||[];
const items=box.querySelector(".items"),tpl=box.querySelector("template");items.innerHTML="";
list.forEach((row,i)=>{const div=document.createElement("div");div.className="item";div.innerHTML=tpl.innerHTML;
div.querySelectorAll("[data-k]").forEach(el=>{if(el.dataset.k.startsWith("."))el.dataset.k=path+"."+i+el.dataset.k;});
div.querySelectorAll("[data-types]").forEach(t=>t.hidden=!t.dataset.types.split(" ").includes(row.type));
const block=(X.blocks||{})[row.template]||{};div.querySelectorAll("[data-k*='.params.']").forEach(el=>{
const k=el.dataset.k.split(".params.")[1];el.placeholder=block[k]!=null?String(block[k])+" (block)":"";});
div.querySelector("[data-remove]").onclick=()=>{list.splice(i,1);render();changed();};items.appendChild(div);});
box.querySelector("[data-add]").onclick=()=>{const n=JSON.parse(box.dataset.new);if(!get(path))set(path,[]);
get(path).push(n);render();changed();};});}
function behavior(){const box=form.querySelector(".behavior");if(!box)return;const study=S.kind!=="home";
const names=study?["battery","ev","thermostat"]:(S.devices||[]).map(d=>d.name);
const typeOf=n=>study?({ev:"ev_v2x"}[n]||n):((S.devices||[]).find(d=>d.name===n)||{}).type;
if(!S.behavior)S.behavior={};box.innerHTML=names.map(n=>{const opts=["",...(POL[typeOf(n)]||[])];
return `<label class="f"><span class="lab">${n}</span><select data-behavior="${n}">`+opts.map(o=>
`<option value="${o}" ${S.behavior[n]===o?"selected":""}>${o||"passive (no rule)"}</option>`).join("")+"</select></label>";
}).join("")||'<p class="note">No devices yet.</p>';box.querySelectorAll("select").forEach(s=>s.onchange=()=>{
if(s.value)S.behavior[s.dataset.behavior]=s.value;else delete S.behavior[s.dataset.behavior];changed();});}
function without(){const box=form.querySelector(".without");if(!box||!S.value||!S.value.cost_test)return;
const ct=S.value.cost_test;if(!ct.without)ct.without={};const study=S.kind!=="home";
const items=study?[["battery","battery"],["ev","ev_v2x"],["thermostat","thermostat"]]:
(S.devices||[]).map(d=>[d.name,d.type]);const deflt={ev_v2x:"tou_timer",thermostat:"rule_based",curtailable_load:"rule_based"};
box.innerHTML=items.map(([n,t])=>{const modes=X.modes[t]||[];const first=deflt[t]?`default (${deflt[t]})`:"pick one (required)";
return `<label class="f"><span class="lab">${n}</span><select data-without="${n}"><option value="">${first}</option>`+
modes.map(m=>`<option ${ct.without[n]===m?"selected":""}>${m}</option>`).join("")+"</select></label>";}).join("")+
(S.solar_from_data&&!study?'<p class="note">solar: unchanged (it has nothing to decide)</p>':"")||
'<p class="note">No devices yet.</p>';box.querySelectorAll("select").forEach(sel=>sel.onchange=()=>{
if(sel.value)ct.without[sel.dataset.without]=sel.value;else delete ct.without[sel.dataset.without];changed();});}
function rulesBox(){const box=form.querySelector(".rules");if(!box)return;const R=S.participation.rules=S.participation.rules||{};
const kinds=["ev","battery","thermostat","solar"];let h='<div class="rules-grid"><span></span>'+
Object.keys(X.rules).map(m=>`<span class="head">${m.replace("_"," ")}</span>`).join("");
kinds.forEach(k=>{h+=`<span>${k}</span>`;Object.keys(X.rules).forEach(m=>{const acts=X.rules[m][k];
if(!acts){h+="<span></span>";return;}const cur=((R[m]||{})[k]||{}).action||"";
h+=`<select data-rule="${m}.${k}"><option value="">default</option>`+acts.map(a=>`<option ${a===cur?"selected":""}>${a}</option>`)
.join("")+"</select>";});});box.innerHTML=h+"</div>";box.querySelectorAll("select").forEach(sel=>sel.onchange=()=>{
const[m,k]=sel.dataset.rule.split(".");R[m]=R[m]||{};if(sel.value)R[m][k]={action:sel.value};else delete R[m][k];changed();});}
const opt=(v,label,on)=>`<option value="${v.replace(/"/g,"&quot;")}" ${on?"selected":""}>`+
label.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))+"</option>";
function picks(){form.querySelectorAll("select[data-pick]").forEach(sel=>{const key=sel.dataset.pick;
let list=sel.dataset.files;if(list==="tariffs"&&S.kind!=="home")list="tariff_dirs";
const files=(X.files||{})[list]||[],input=sel.parentElement.querySelector("input"),cur=get(key);
if(sel.dataset.add){sel.innerHTML=opt("","add a file…",true)+files.map(f=>opt(f,f,false)).join("");input.hidden=false;
sel.onchange=()=>{if(sel.value){const l=get(key)||[];if(!l.includes(sel.value))l.push(sel.value);set(key,l);
show(input,l);changed();}sel.value="";};return;}
const known=cur!=null&&files.includes(cur),other=cur!=null&&cur!==""&&!known;
sel.innerHTML=opt("","choose…",cur==null)+files.map(f=>opt(f,f,f===cur)).join("")+opt("__other","another path…",other);
input.hidden=!other;sel.onchange=()=>{if(sel.value==="__other"){input.hidden=false;input.value="";input.focus();}
else{input.hidden=true;set(key,sel.value||null);show(input,get(key));changed();}};});}
function render(){rows();behavior();without();rulesBox();
const ev=form.querySelector("select[data-events]");if(ev&&S.dr&&S.dr.events){const k=Object.keys(S.dr.events)[0]||"random";
ev.value=k.replace("_yaml","");}form.querySelectorAll("[data-k]").forEach(el=>show(el,el.dataset.k==="_target"?
S._target:get(el.dataset.k)));form.querySelectorAll("[data-enable]").forEach(cb=>{cb.checked=get(cb.dataset.enable)!=null;});
form.querySelectorAll("[data-needs]").forEach(d=>d.hidden=get(d.dataset.needs)==null);
form.querySelectorAll("[data-when]").forEach(d=>d.hidden=!holds(d.dataset.when));picks();
const allowed=KINDS[S.kind]||KINDS.home;document.querySelectorAll("[data-goto]").forEach(a=>{
a.hidden=!allowed.includes(+a.dataset.goto);a.classList.toggle("on",+a.dataset.goto===step);});
form.querySelectorAll("[data-kinds]").forEach(d=>d.hidden=!d.dataset.kinds.split(" ").includes(S.kind)||
(d.dataset.when!==undefined&&!holds(d.dataset.when)));
form.querySelectorAll("section.step").forEach(s=>s.hidden=+s.dataset.step!==step);}
function changed(){clearTimeout(timer);timer=setTimeout(preview,250);}
async function preview(){const mine=++seq;const body=new URLSearchParams({answers:JSON.stringify(S),target:S._target||""});
const r=await fetch("/create/preview",{method:"POST",headers:{"X-Token":TOKEN},body});if(mine!==seq)return;
const d=await r.json();document.querySelector("pre.yaml").textContent=d.text||"";const by={};
d.items.forEach(i=>(by[i.step]=by[i.step]||[]).push(i));const li=i=>`<li class="lv-${i.level}">${i.level}: `+
i.message.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))+"</li>";
document.querySelectorAll("[data-checks]").forEach(b=>{const list=by[b.dataset.checks]||[];
b.innerHTML=list.length?"<ul>"+list.map(li).join("")+"</ul>":"";});
document.querySelectorAll("[data-goto]").forEach(a=>{const list=by[a.dataset.goto]||[];const e=list.filter(i=>i.level==="error").length;
const badge=a.querySelector(".badge");badge.textContent=list.length||"";badge.className="badge"+(e?" err":list.length?" warn":"");});
const errors=d.items.filter(i=>i.level==="error").length,warnings=d.items.length-errors;
document.querySelector(".preview .summary").innerHTML=errors?`<p class="status bad">${errors} error(s) to fix before
writing${warnings?`, ${warnings} warning(s)`:""}.</p>`:`<p class="status ok">Ready to write${warnings?` (${warnings} warning(s))`:""}.</p>`;
document.querySelector(".all-checks").innerHTML=d.items.length?"<ul>"+d.items.map(i=>`<li class="lv-${i.level}">step ${i.step}: `+
i.message.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))+"</li>").join("")+"</ul>":"<p>Every check is clean.</p>";
document.querySelectorAll("[data-write]").forEach(b=>b.disabled=errors>0);}
form.addEventListener("input",ev=>{const el=ev.target;if(!el.dataset.k)return;
if(el.dataset.k==="_target")S._target=el.value;else set(el.dataset.k,value(el));if(el.hasAttribute("data-rerender"))render();changed();});
form.addEventListener("change",ev=>{const el=ev.target;if(el.hasAttribute("data-events")){
S.dr.events=JSON.parse(JSON.stringify(DEF.events[el.value]));render();changed();return;}
if(el.dataset.t==="bool"&&el.dataset.k){set(el.dataset.k,el.checked);render();changed();return;}
if(el.dataset.enable){const key=el.dataset.defaultKey||el.dataset.enable;
set(el.dataset.enable,el.checked?JSON.parse(JSON.stringify(DEF[key])):null);render();changed();}
else if(el.dataset.k&&(el.hasAttribute("data-rerender")||el.tagName==="SELECT")){render();}});
document.querySelectorAll("[data-goto]").forEach(a=>a.onclick=ev=>{ev.preventDefault();step=+a.dataset.goto;render();});
const move=d=>{const allowed=KINDS[S.kind]||KINDS.home;const i=allowed.indexOf(step);step=allowed[Math.max(0,Math.min(
allowed.length-1,i+d))];render();window.scrollTo(0,0);};
form.querySelector("[data-prev]").onclick=()=>move(-1);form.querySelector("[data-next]").onclick=()=>move(1);
document.querySelectorAll("[data-preset]").forEach(b=>b.onclick=async()=>{const r=await fetch("/create/preset?name="+
b.dataset.preset);S=await r.json();step=1;render();changed();});
document.querySelectorAll("[data-write]").forEach(b=>b.onclick=async()=>{const body=new URLSearchParams({
answers:JSON.stringify(S),target:S._target||"",run:b.dataset.write||""});const r=await fetch("/create/write",
{method:"POST",headers:{"X-Token":TOKEN},body});if(r.headers.get("X-Location")){location=r.headers.get("X-Location");return;}
document.getElementById("write-result").innerHTML=await r.text();});
render();changed();})();
"""
