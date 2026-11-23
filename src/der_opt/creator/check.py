"""Checking the creator's answers as you go (docs/SCENARIO_CREATOR_SPEC.md v1.0,
section 3): the file as it would be written, run through `der-opt validate` without
solving, each finding filed under the step it belongs to; plus what the data must hold
for the devices picked."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from der_opt.creator.answers import Answers, steps_for
from der_opt.creator.writer import compose_answers

# a finding's step: by the scenario key its message names, then by its check
KEY_STEPS = (("baseline_behavior", 9), ("response: rules", 9), ("participation", 9), ("analyses.feeder.coordination", 11), ("analyses.feeder.elements", 4), ("analyses.feeder.value_streams", 12),
             ("analyses.portfolio.dr", 10), ("analyses.portfolio.cost_test", 12), ("analyses.portfolio", 12),
             ("time_grid", 2), ("population", 3), ("premises", 4), ("external_load", 3), ("devices", 5),
             ("add_devices", 5), ("value_streams", 6), ("objective", 6), ("billing", 6), ("tariffs", 6),
             ("poi_limit_kw", 7), ("import_limit_kw", 7), ("export_limit_kw", 7), ("exclusive_import_export", 7),
             ("counterfactual", 8), ("dr", 10), ("cost_test", 12), ("avoided_costs", 12), ("defaults", 5))
CHECK_STEPS = {"dr calendar": 10, "cost test": 12, "cases": 12, "billing": 6, "tariff dates": 6, "price units": 6,
               "device names": 5, "ev pattern": 5, "load": 3, "population": 3, "coordination": 11, "files": 3,
               "time grid": 2, "data": 3, "avoided costs": 12}


@dataclass
class Item:
    step: int
    level: str  # error, warning
    message: str


def step_of(check: str, message: str, kind_steps: list[int]) -> int:
    for key, step in KEY_STEPS:
        if key in message and step in kind_steps:
            return step
    step = CHECK_STEPS.get(check, 13)
    return step if step in kind_steps else 13


def data_items(a: Answers, base: Path) -> list[Item]:
    """What the data must hold for what's picked, before anything else reads it."""
    import pandas as pd

    items: list[Item] = []
    if a.study:
        for label, value, step in (("premises file", a.data.premises, 3), ("network file", a.data.network, 4)):
            if value and not (base / value).exists():
                items.append(Item(step, "error", f"the {label} {value} isn't there"))
        return items
    if a.prices.objective == "lmp":
        return items
    if not a.data.timeseries:
        return [Item(3, "error", "a time series file (load_kw) is needed: the home's load comes from it")]
    path = base / a.data.timeseries
    if not path.is_file():
        return [Item(3, "error", f"the time series file {a.data.timeseries} isn't there")]
    columns = set(pd.read_csv(path, nrows=0).columns)
    needs = {"load_kw": 3}
    if a.solar_from_data:
        needs["pv_kw"] = 5
    for device in a.devices:
        if device.type == "thermostat" and "ambient_temp_c" not in device.params:
            needs["outdoor_temp_c"] = 5
    for column, step in needs.items():
        if column not in columns:
            what = {"pv_kw": "solar from the data", "outdoor_temp_c": "a thermostat (its outdoor temperature)"}.get(
                column, "the home's load")
            items.append(Item(step, "error", f"{a.data.timeseries} has no {column} column, which {what} needs"))
    if "premise_id" in columns and not a.data.premise:
        ids = pd.read_csv(path, usecols=["premise_id"])["premise_id"].unique()
        if len(ids) > 1:
            items.append(Item(3, "error", f"pick the premise: the file has {len(ids)} ({', '.join(map(str, ids[:5]))}"
                                          f"{'...' if len(ids) > 5 else ''})"))
    return items


def check(a: Answers, target: Path, base: Path, library: Path | None = None) -> tuple[str, list[Item]]:
    """(the file's text, every finding by step). Nothing is written but a scratch copy
    beside the target, removed after."""
    from der_opt.scenarios.validate import validate_scenario

    kind_steps = steps_for(a.kind)
    try:
        text, _ = compose_answers(a, target, base, library)
    except (ValueError, KeyError) as exc:
        return "", [Item(5, "error", str(exc))]
    items = data_items(a, base)
    scratch = target.parent / f".{target.stem}.creator-check.yaml"
    try:
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(text, encoding="utf-8")
        report = validate_scenario(scratch, before_run=True).report
    finally:
        scratch.unlink(missing_ok=True)
    seen = {item.message for item in items}
    data_errors = any(item.level == "error" for item in items)
    grouped: dict[tuple, list[str]] = {}  # the same finding at many premises, once
    for f in report.findings:
        if f.level == "info" or f.message in seen or (data_errors and f.check == "build"):
            continue  # a build that fails on data already named above
        message = f.message.replace(scratch.name, target.name)
        key = (step_of(f.check, message, kind_steps), f.level, message)
        grouped.setdefault(key, []).extend([f.premise] if f.premise else [])
    for (step, level, message), premises in grouped.items():
        if premises:
            who = premises[0] if len(premises) == 1 else f"{premises[0]} and {len(premises) - 1} more"
            message = f"{who}: {message}"
        items.append(Item(step, level, message))
    return text, items
