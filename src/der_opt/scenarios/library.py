"""Picking blocks from a library (docs/SCENARIO_COMPOSITION_SPEC.md section 3a):
`der-opt blocks` lists them, and `der-opt new --pick` / `--use` writes a single-home
scenario that extends the ones chosen.

A block's kind comes from what it sets (`time_grid`, `devices`, `dr`...), not its
folder, which is only a convention. Picks that don't fit together are refused, with
nothing written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from der_opt.scenarios.compose import CompositionError, compose_scenario, key_path

STARTER_LIBRARY = Path(__file__).resolve().parents[3] / "examples" / "library"
SCENARIO_TODO = "set in the scenario"  # a block placeholder that each home fills in
HEADERS = ("# der-opt starter library block", "# A der-opt scenario fragment")


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    many: bool  # pick any number, or at most one
    required: bool = False


KINDS = [
    Kind("time", "Time grid", many=False, required=True),
    Kind("devices", "Devices", many=True),
    Kind("value_streams", "Prices and objective", many=False, required=True),
    Kind("meter", "Meter limits", many=True),
    Kind("counterfactual", "Counterfactual (R1)", many=True),
    Kind("billing", "Billing", many=False),
    Kind("dr", "DR program", many=False),
    Kind("dr_addon", "DR add-ons (after the program)", many=True),
    Kind("avoided_costs", "Avoided costs (screening only)", many=False),
    Kind("cost_test", "Cost tests", many=False),
    Kind("other", "Other", many=True),
]
METER_KEYS = {"poi_limit_kw", "import_limit_kw", "export_limit_kw", "exclusive_import_export"}
FEEDER_KEYS = {"house_defaults", "feeder", "houses", "premise_data"}  # the old feeder form
STUDY_KEYS = {"population", "premises", "tariffs", "objective", "defaults", "overrides", "add_devices", "analyses"}


@dataclass
class Block:
    path: Path
    name: str  # folder/stem, relative to the library
    kind: str
    description: str
    data: dict  # the block composed alone (with what it extends)
    needs: list[str] = field(default_factory=list)  # dotted keys it leaves as TODO
    illustrative: bool = False

    @property
    def stem(self) -> str:
        return self.path.stem


def _kind(data: dict) -> str:
    keys = set(data)
    if keys & STUDY_KEYS:
        return "study"
    if keys & FEEDER_KEYS:
        return "feeder"
    if keys == {"time_grid"}:
        return "time"
    if keys == {"devices"}:
        return "devices"
    if keys == {"value_streams"}:
        return "value_streams"
    if keys and keys <= METER_KEYS:
        return "meter"
    if keys == {"counterfactual"}:
        return "counterfactual"
    if keys == {"billing"}:
        return "billing"
    if keys == {"dr"}:
        return "dr" if (data["dr"].get("program") or {}).get("program_id") else "dr_addon"
    if keys == {"avoided_costs"}:
        return "avoided_costs"
    if keys == {"cost_test"}:
        return "cost_test"
    return "other"


def _description(path: Path, data: dict) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        if not line.startswith(HEADERS):
            return line.lstrip("# ").strip()
    return ", ".join(data) or "(empty)"


def _todos(data, trail: str = "") -> list[tuple[str, str]]:
    found = []
    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []
    for key, value in items:
        where = key_path(trail, key)
        if isinstance(value, str) and "TODO" in value:
            found.append((where, value))
        else:
            found.extend(_todos(value, where))
    return found


def _illustrative(path: Path) -> bool:
    return any("illustrative" in line.split("#", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()
               if "#" in line and not line.lstrip().startswith("#"))


def read_library(folder: str | Path) -> list[Block]:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} isn't a folder")
    blocks = []
    for path in sorted(folder.rglob("*.yaml")):
        try:
            data = compose_scenario(path).data
        except CompositionError:
            continue  # not a block; validate reports broken ones in the scenarios that use them
        name = path.relative_to(folder).with_suffix("").as_posix()
        illustrative = any(_illustrative(p) for p in [path, *compose_scenario(path).fragments])
        blocks.append(Block(path, name, _kind(data), _description(path, data), data,
                            [w for w, _ in _todos(data)], illustrative))
    return blocks


def find(blocks: list[Block], wanted: str) -> Block:
    """A block by folder/name or, when unique, by name alone."""
    wanted = wanted.removesuffix(".yaml")
    exact = [b for b in blocks if b.name == wanted]
    if exact:
        return exact[0]
    by_stem = [b for b in blocks if b.stem == wanted]
    if len(by_stem) == 1:
        return by_stem[0]
    if by_stem:
        raise ValueError(f"{wanted!r} is in several folders; write one of: {', '.join(b.name for b in by_stem)}")
    raise ValueError(f"no block {wanted!r} in the library (`der-opt blocks` lists them)")


# --- what a pick needs -------------------------------------------------------------


@dataclass
class HomeData:
    """Devices and load from a time series file: solar from pv_kw, load from load_kw
    (or net_kw for a metered home)."""

    path: Path | None = None
    premise: str | None = None
    columns: list[str] = field(default_factory=list)

    def device_names(self) -> list[str]:
        return ["solar"] if "pv_kw" in self.columns else []


def problems(picks: list[Block], home: HomeData) -> list[str]:
    """Why these blocks can't make a scenario; empty if they can."""
    found = []
    by_kind: dict[str, list[Block]] = {}
    for block in picks:
        by_kind.setdefault(block.kind, []).append(block)
    for block in by_kind.get("study", []):
        found.append(f"{block.name} is for studies (`der-opt new feeder-from-files`), not a single home")
    for block in by_kind.get("feeder", []):
        found.append(f"{block.name} is for feeder scenarios, not a single home")
    for kind in KINDS:
        chosen = by_kind.get(kind.key, [])
        if kind.required and not chosen:
            found.append(f"pick a {kind.title.lower()} block")
        if not kind.many and len(chosen) > 1:
            found.append(f"pick one {kind.title.lower()} block, not {len(chosen)} ({', '.join(b.name for b in chosen)})")

    devices: dict[str, str] = dict.fromkeys(home.device_names(), "the time series file")
    for block in by_kind.get("devices", []):
        for device in block.data.get("devices", []):
            name = device.get("name")
            if name in devices:
                found.append(f"{block.name} and {devices[name]} both have a device named {name!r}; pick one")
            devices[name] = block.name
    for block in by_kind.get("devices", []):
        for device in block.data.get("devices", []):
            missing = sorted(set(device.get("params", {}).get("charge_sources", [])) - set(devices))
            if missing:
                found.append(f"{block.name}: {device['name']} charges from {missing}, which you haven't picked")

    for block in by_kind.get("counterfactual", []):
        for name in block.data["counterfactual"].get("policies", {}):
            if name not in devices:
                found.append(f"{block.name} sets a counterfactual for {name!r}, which isn't one of your devices "
                             f"({', '.join(devices) or 'none'})")

    meter: dict[str, tuple[object, str]] = {}
    for block in by_kind.get("meter", []):
        for key, value in block.data.items():
            if key in meter and meter[key][0] != value:
                found.append(f"{block.name} and {meter[key][1]} both set {key} ({value} vs {meter[key][0]}); pick one")
            meter[key] = (value, block.name)

    streams = [s.get("type") for b in by_kind.get("value_streams", []) for s in b.data["value_streams"]]
    if {"bill", "nbt_bill", "nem2_bill"} & set(streams) and not by_kind.get("billing"):
        found.append("the bill objective needs a billing block (it optimizes that bill)")

    if by_kind.get("dr_addon") and not by_kind.get("dr"):
        found.append(f"{by_kind['dr_addon'][0].name} adds to a DR program; pick a program too")
    for kind, section in (("dr", "dr"),):
        for block in by_kind.get(kind, []):
            measure = block.data[section].get("measure_devices") or []
            missing = sorted(set(measure) - set(devices))
            if missing:
                found.append(f"{block.name} counts {missing} as the measure, which you haven't picked")
    return found


def ordered(picks: list[Block]) -> list[Block]:
    """Picks in merge order: by kind (DR add-ons after the program), then as picked."""
    rank = {k.key: i for i, k in enumerate(KINDS)}
    return sorted(picks, key=lambda b: rank.get(b.kind, len(rank)))


def _scenario_stubs(picks: list[Block]) -> tuple[list[dict], dict, list[str]]:
    """What the scenario must fill in for the blocks it extends: device settings by
    name, other settings by key, and study-level TODOs left in the blocks."""
    devices: dict[str, dict] = {}
    others: dict = {}
    in_blocks: list[str] = []
    for block in picks:
        for where, value in _todos(block.data):
            if SCENARIO_TODO not in value:
                in_blocks.append(f"{where} in {block.path.name}")
                continue
            if where.startswith("devices["):
                index = int(where[len("devices["):where.index("]")])
                name = block.data["devices"][index]["name"]
                rest = where[where.index("]") + 2:]
                target = devices.setdefault(name, {"name": name})
            else:
                rest, target = where, others
            parts = rest.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
    return list(devices.values()), others, in_blocks


@dataclass
class Picked:
    scenario: Path
    blocks: list[Block]
    todos_in_scenario: int
    todos_in_blocks: list[str]
    command: str


def write_picked(target: Path, picks: list[Block], home: HomeData, library: Path, force: bool = False) -> Picked:
    from der_opt.scenarios.templates import _path_text, _relative

    target = Path(target)
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass --force to overwrite it")
    found = problems(picks, home)
    if found:
        raise ValueError("these blocks don't fit together, so nothing was written:\n" + "\n".join(f"  - {p}" for p in found))
    picks = ordered(picks)
    device_stubs, other_stubs, in_blocks = _scenario_stubs(picks)

    own: dict = {"name": target.stem.replace("-", "_")}
    devices: list[dict] = []
    if home.path is not None:
        premise = home.premise or "TODO"
        source = {"source": "timeseries", "path": _path_text(home.path, target.parent), "premise_id": premise}
        if "pv_kw" in home.columns:
            devices.append({"type": "solar", "name": "solar", "params": {"available_kw": source | {"column": "pv_kw"}}})
    devices += device_stubs
    if devices:
        own["devices"] = devices
    if home.path is not None:
        column = "load_kw" if "load_kw" in home.columns else "net_kw"
        own["external_load"] = source | {"column": column}
    own |= other_stubs

    lines = [f"# Written by `der-opt new --use` from {library}.",
             f"# Replace every TODO, then check it with:  der-opt validate {target.name}",
             "# Relative paths are from this file's folder. Change a block's device by naming it here:",
             "#   devices: [{name: battery, params: {capacity_kwh: 10}}]", "",
             "extends:"]
    lines += [f"  - {_relative(b.path, target.parent)}" for b in picks]
    lines.append("")
    body = "\n".join(
        yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=10_000,
                       default_flow_style=None if isinstance(value, (dict, list)) else False)
        for key, value in own.items())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")

    parts = [f"der-opt new {target}"] + [f"--use {b.name}" for b in picks]
    if home.path is not None:
        parts.append(f"--timeseries {home.path}")
        if home.premise:
            parts.append(f"--premise {home.premise}")
    todos = sum(1 for _, v in _todos(own) if "TODO" in v)
    return Picked(target, picks, todos, in_blocks, " ".join(parts))


def home_data(path: Path | None, premise: str | None) -> HomeData:
    if path is None:
        return HomeData()
    from der_opt.inputs import read_timeseries

    data = read_timeseries(path, strict=False)
    premises = sorted(data.premises) if data.premises else []
    if premise is None and len(premises) == 1:
        premise = premises[0]
    return HomeData(Path(path), premise, list(data.columns))


def time_matches(block: Block, home: HomeData) -> bool:
    """The block's time grid is exactly the time series file's span."""
    from der_opt.scenarios.templates import _timeseries_facts

    if block.kind != "time" or home.path is None:
        return False
    _, grid = _timeseries_facts(home.path)
    tg = block.data["time_grid"]
    return grid is not None and (str(tg.get("start")), tg.get("resolution_minutes"), tg.get("horizon_steps")) == grid
