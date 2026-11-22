"""Scenario building blocks (docs/SCENARIO_COMPOSITION_SPEC.md v1.0): `extends` merges
fragment files into a scenario, and every relative path resolves from the folder of
the file that names it.

The result is plain data, with every path made absolute, ready for the scenario schema.
Nothing downstream needs to know a scenario was assembled from pieces.
"""

from __future__ import annotations

import hashlib
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Keys whose string values (or lists of strings) are file or folder paths. Fixed, so
# nothing is guessed from what a value looks like.
PATH_KEYS = frozenset({
    "path", "csv_path", "export_rates_csv", "tariff", "tariff_dir", "urdb_json_path",
    "scenario_dir", "arrivals_csv", "departures_csv", "set", "file", "premises", "network", "timeseries",
    "template",
})
MERGE_BY_NAME = frozenset({"devices"})


class CompositionError(ValueError):
    """A scenario's `extends` can't be resolved: a missing fragment, a cycle, or a
    fragment that isn't a mapping."""


class OldStylePathWarning(UserWarning):
    """A relative path that only works from the current folder (the old rule). It
    becomes an error in the next release."""


@dataclass
class ComposedScenario:
    path: Path  # the scenario file
    data: dict  # merged, with every path absolute
    fragments: list[Path] = field(default_factory=list)  # every file extended, in merge order
    path_warnings: list[str] = field(default_factory=list)

    def fingerprints(self) -> list[dict]:
        return [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in self.fragments]


def _is_pathlike(value: str) -> bool:
    return bool(value) and "://" not in value and "TODO" not in value  # placeholders stay as written


def _resolve(value: str, base: Path, where: str, notes: list[str]) -> str:
    """`value` as an absolute path: from `base` (the containing file's folder). If it
    only exists from the current folder, that's used for now, with a warning."""
    if not _is_pathlike(value):
        return value
    given = Path(value).expanduser()
    if given.is_absolute():
        return str(given)
    beside = Path(os.path.normpath(base / given))
    if beside.exists():
        return str(beside)
    from_cwd = Path.cwd() / given
    if from_cwd.exists():
        notes.append(f"{where}: {value!r} is relative to the current folder, not to the file that names it; "
                     f"write it as {Path(os.path.relpath(from_cwd, base)).as_posix()!r} (old-style paths stop "
                     "working in the next release)")
        return str(Path(os.path.normpath(from_cwd)))
    return str(beside)  # missing; validate reports it, naming where it looked


def key_path(prefix: str, key) -> str:
    """Dotted key paths: dr.program.window, devices[0].params."""
    if isinstance(key, int):
        return f"{prefix}[{key}]"
    return f"{prefix}.{key}" if prefix else str(key)


def resolve_paths(data, base: Path, source: Path, notes: list[str], trail: str = ""):
    """A copy of `data` with every value under a path key made absolute."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            here = key_path(trail, key)
            where = f"{source.name}: {here}"
            if key in PATH_KEYS and isinstance(value, str):
                out[key] = _resolve(value, base, where, notes)
            elif key in PATH_KEYS and isinstance(value, list) and all(isinstance(v, str) for v in value):
                out[key] = [_resolve(v, base, where, notes) for v in value]
            else:
                out[key] = resolve_paths(value, base, source, notes, here)
        return out
    if isinstance(data, list):
        return [resolve_paths(v, base, source, notes, key_path(trail, i)) for i, v in enumerate(data)]
    return data


def merge(base, over, key: str | None = None):
    """`over` on top of `base`: mappings key by key at every depth; `devices` by name,
    a device with an inherited name merging into it key by key (unless it's another
    type, which replaces it); every other list (and value) replaced. `null` removes an
    inherited key."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            if v is None and k in out:
                del out[k]
            elif k in out:
                out[k] = merge(out[k], v, k)
            else:
                out[k] = v
        return out
    if key in MERGE_BY_NAME and isinstance(base, list) and isinstance(over, list):
        out = list(base)
        position = {d.get("name"): i for i, d in enumerate(out) if isinstance(d, dict)}
        for device in over:
            name = device.get("name") if isinstance(device, dict) else None
            if name is not None and name in position:
                inherited = out[position[name]]
                same_type = device.get("type") in (None, inherited.get("type"))
                out[position[name]] = merge(inherited, device) if same_type else device
            else:
                position[name] = len(out)
                out.append(device)
        return out
    return over


def _read(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CompositionError(f"{path} doesn't exist") from exc
    except yaml.YAMLError as exc:
        raise CompositionError(f"{path} isn't valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CompositionError(f"{path} must be a mapping of scenario sections, not a {type(raw).__name__}")
    return raw


def _compose(path: Path, stack: tuple[Path, ...], fragments: list[Path], notes: list[str]) -> dict:
    if path in stack:
        chain = " -> ".join(p.name for p in (*stack, path))
        raise CompositionError(f"extends goes in a circle: {chain}")
    raw = _read(path)
    parents = raw.pop("extends", None) or []
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
        raise CompositionError(f"{path.name}: extends must be a file or a list of files")
    merged: dict = {}
    for parent in parents:
        target = Path(_resolve(parent, path.parent, f"{path.name}: extends", notes))
        if not target.is_file():
            raise CompositionError(f"{path.name}: extends {parent!r}, which doesn't exist ({target})")
        merged = merge(merged, _compose(target, (*stack, path), fragments, notes))
        if target not in fragments:
            fragments.append(target)
    return merge(merged, resolve_paths(raw, path.parent, path, notes))


def compose_scenario(path: str | Path) -> ComposedScenario:
    """Reads a scenario, merges what it extends, and resolves its paths."""
    path = Path(path).expanduser().resolve()
    fragments: list[Path] = []
    notes: list[str] = []
    data = _compose(path, (), fragments, notes)
    return ComposedScenario(path, data, fragments, list(dict.fromkeys(notes)))


def load_composed(path: str | Path) -> ComposedScenario:
    """compose_scenario, raising each old-style path as an OldStylePathWarning."""
    composed = compose_scenario(path)
    for note in composed.path_warnings:
        warnings.warn(note, OldStylePathWarning, stacklevel=3)
    return composed
