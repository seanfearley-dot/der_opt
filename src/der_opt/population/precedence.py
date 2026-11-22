"""One precedence rule for every setting (docs/POPULATION_MODEL_SPEC.md section 3).

Layers merge weakest first: built-in, template, defaults, row, override. Mappings merge
key by key at every depth, and `null` removes a setting. A data source or price series
(a mapping with a `source` or `type` key) and every list are one value: a stronger
layer replaces them whole. Every resolved value records which layer set it.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("built-in", "template", "defaults", "row", "override")


@dataclass(frozen=True)
class Layer:
    level: str  # one of LEVELS
    source: str  # where it came from, for the record: "template:commuter_charger", "premises.csv row 4"
    values: dict

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"unknown precedence level {self.level!r}; choose from {LEVELS}")


def is_atomic(value) -> bool:
    """A value a stronger layer replaces whole, rather than merging into."""
    return not isinstance(value, dict) or "source" in value or "type" in value


def _merge(out: dict, sources: dict, values: dict, source: str, path: str) -> None:
    for key, value in values.items():
        here = f"{path}.{key}" if path else str(key)
        if value is None:
            out.pop(key, None)
            for leaf in [k for k in sources if k == here or k.startswith(here + ".")]:
                del sources[leaf]
            continue
        if not is_atomic(value) and isinstance(out.get(key), dict) and not is_atomic(out[key]):
            _merge(out[key], sources, value, source, here)
            continue
        for leaf in [k for k in sources if k.startswith(here + ".")]:
            del sources[leaf]
        out[key] = _copy(value)
        if is_atomic(value):
            sources[here] = source
        else:
            _record(sources, value, source, here)


def _record(sources: dict, value: dict, source: str, path: str) -> None:
    for key, item in value.items():
        here = f"{path}.{key}"
        if is_atomic(item):
            sources[here] = source
        else:
            _record(sources, item, source, here)


def _copy(value):
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def resolve(layers: list[Layer]) -> tuple[dict, dict[str, str]]:
    """The merged values, and the source of each leaf setting (dotted path)."""
    order = {level: i for i, level in enumerate(LEVELS)}
    out: dict = {}
    sources: dict[str, str] = {}
    for layer in sorted(layers, key=lambda layer: order[layer.level]):  # stable: same-level layers keep their order
        _merge(out, sources, layer.values, layer.source, "")
    return out, sources
