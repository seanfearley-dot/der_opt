"""Device templates (docs/POPULATION_MODEL_SPEC.md section 2): a named set of parameters
for one device type, with its counterfactual policy and coordination rules.

Two forms are read:

  template: commuter_charger        # optional; default the file's name
  type: ev
  params: {...}
  counterfactual: {type: rule_based_ev}
  rules: {dr_event: {action: pause}, envelope: {action: limit}}

or a library device block holding one device (`devices: [{type, name, params}]`), so the
starter library's blocks work as templates unchanged. Templates can `extends` each
other, like any block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# the spec's type names, and the device types scenarios use today
TYPE_NAMES = {"ev": "ev_v2x", "flexible_load": "curtailable_load"}
KINDS = {"solar": "solar", "battery": "battery", "ev_v2x": "ev", "thermostat": "thermostat",
         "curtailable_load": "flexible_load"}


def config_type(name: str) -> str:
    """A device type as scenarios write it (ev -> ev_v2x)."""
    return TYPE_NAMES.get(name, name)


@dataclass(frozen=True)
class Template:
    name: str
    type: str  # a scenario device type: solar, battery, ev_v2x, thermostat, curtailable_load
    params: dict
    path: Path
    counterfactual: dict | None = None
    rules: dict = field(default_factory=dict)  # mechanism -> rule

    @property
    def source(self) -> str:
        return f"template:{self.name}"


@lru_cache(maxsize=256)
def _load(path: str, mtime: float) -> Template:
    from der_opt.scenarios.compose import compose_scenario

    data = compose_scenario(path).data
    file = Path(path)
    if "devices" in data and "type" not in data:
        devices = data["devices"]
        if not isinstance(devices, list) or len(devices) != 1:
            raise ValueError(f"{file.name}: a device block used as a template must hold exactly one device")
        device = devices[0]
        return Template(file.stem, config_type(device["type"]), dict(device.get("params") or {}), file)
    if "type" not in data:
        raise ValueError(f"{file.name} isn't a device template: it needs `type` and `params` "
                         "(or a devices list with one device)")
    unknown = set(data) - {"template", "type", "params", "counterfactual", "rules"}
    if unknown:
        raise ValueError(f"{file.name}: a template has template, type, params, counterfactual and rules; "
                         f"not {sorted(unknown)}")
    kind = config_type(data["type"])
    if kind not in KINDS:
        raise ValueError(f"{file.name}: type {data['type']!r} isn't a device type ({', '.join(KINDS)})")
    name = Path(str(data["template"])).name if data.get("template") else file.stem  # a path-looking name: its last part
    return Template(name, kind, dict(data.get("params") or {}), file,
                    data.get("counterfactual"), dict(data.get("rules") or {}))


def load_template(path: str | Path) -> Template:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"device template {path} doesn't exist")
    return _load(str(path), path.stat().st_mtime)
