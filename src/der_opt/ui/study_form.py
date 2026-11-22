"""The study settings forms (docs/REPORTS.md section 3): a study's own `defaults`,
`overrides` and `add_devices` as rows of setting and value, and back.

A value is written as YAML (`0.9`, `[solar]`, `{type: self_consumption}`), so any
setting fits a row. Saving rewrites only these three sections of the file.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from der_opt.ui.sections import flow, replace_sections

SECTIONS = ("defaults", "overrides", "add_devices")
FIRST_DEVICE_KEYS = ("device_id", "type", "template", "kw", "kwh", "v2x", "installed_cost", "annual_om")


def _rows(mapping) -> list[list[str]]:
    return [[str(k), flow(v)] for k, v in (mapping or {}).items()]


def form_state(text: str, composed: dict | None, resolved: list | None, templates: list[str]) -> dict:
    """What the forms start from: the file's own sections, the defaults it inherits,
    and the names to suggest."""
    from der_opt.inputs.spec import DEVICE_COLUMNS, DEVICE_TYPES, device_parameters
    from der_opt.population.compile import DEVICE_EXTRAS
    from der_opt.population.resolved import PREMISE_SETTINGS, ROLE_SETTINGS

    own = yaml.safe_load(text) or {}
    defaults = [{"where": _rows(d.get("where")), "premise": _rows((d.get("set") or {}).get("premise")),
                 "device": _rows((d.get("set") or {}).get("device"))} for d in own.get("defaults") or []]
    overrides = []
    for target, value in (own.get("overrides") or {}).items():
        value = value or {}
        if value.get("premise"):
            overrides.append({"target": str(target), "settings": _rows(value["premise"])})
        if value.get("device"):
            overrides.append({"target": str(target), "settings": _rows(value["device"])})
        for device, settings in (value.get("devices") or {}).items():
            overrides.append({"target": f"{target}/{device}", "settings": _rows(settings)})
    additions = []
    for entry in own.get("add_devices") or []:
        device = dict(entry.get("device") or {})
        ordered = {k: device.pop(k) for k in FIRST_DEVICE_KEYS if k in device} | device
        additions.append({"where": _rows(entry.get("where")), "share": flow(entry.get("share", "")),
                          "seed": flow(entry.get("seed", 0)), "device": _rows(ordered)})
    inherited_all = (composed or {}).get("defaults") or []
    inherited = inherited_all[:max(0, len(inherited_all) - len(own.get("defaults") or []))]

    where_keys = {"premise_id", "element_id", "kind", "customer_class", "tariff", "export_program", "participation",
                  "baseline_behavior", "response", "type", "template", "device_id"}
    targets: list[str] = []
    for r in resolved or []:
        where_keys |= {k for k in r.extra if k != "data"}
        targets.append(r.premise_id)
        targets += [f"{r.premise_id}/{d.device_id}" for d in r.devices]
    device_settings = sorted({p for kind in DEVICE_TYPES for p in device_parameters(kind)} | set(DEVICE_EXTRAS)
                             | {"pattern"})
    premise_settings = sorted({*PREMISE_SETTINGS, *ROLE_SETTINGS, "customer_class", "participant_upfront",
                               "participant_annual_om"} - {"dr", "avoided_costs", "cost_test", "weather_case"})
    return {
        "defaults": defaults, "overrides": overrides, "add_devices": additions,
        "inherited": [flow(d) for d in inherited],
        "suggest": {"where": sorted(where_keys), "premise": premise_settings, "device": device_settings,
                    "targets": targets, "add_device": list(dict.fromkeys([*DEVICE_COLUMNS[1:], *device_settings])),
                    "types": list(DEVICE_TYPES), "templates": templates},
    }


class FormErrors(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _mapping(rows, where: str, errors: list[str]) -> dict:
    out = {}
    for key, text in rows or []:
        key = (key or "").strip()
        if not key and not (text or "").strip():
            continue
        if not key:
            errors.append(f"{where}: a value without a setting name ({text!r})")
            continue
        if not (text or "").strip():
            errors.append(f"{where}: {key} has no value")
            continue
        try:
            out[key] = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"{where}: {key}: {text!r} isn't YAML ({str(exc).splitlines()[0]})")
    return out


def sections_from_form(form: dict) -> dict:
    """The three sections from the forms; FormErrors names every entry that's wrong."""
    from der_opt.scenarios.schema import AddDevicesConfig, StudyDefault

    errors: list[str] = []
    defaults = []
    for i, entry in enumerate(form.get("defaults") or []):
        where = _mapping(entry.get("where"), f"defaults[{i}] where", errors)
        settings = {k: v for k, v in (("premise", _mapping(entry.get("premise"), f"defaults[{i}] premise", errors)),
                                      ("device", _mapping(entry.get("device"), f"defaults[{i}] device", errors))) if v}
        if not settings:
            errors.append(f"defaults[{i}]: set at least one premise or device setting")
            continue
        item = ({"where": where} if where else {}) | {"set": settings}
        try:
            StudyDefault.model_validate(item)
        except ValidationError as exc:
            errors.append(f"defaults[{i}]: {exc.errors()[0]['msg']}")
        defaults.append(item)
    overrides: dict = {}
    for i, entry in enumerate(form.get("overrides") or []):
        target = (entry.get("target") or "").strip()
        settings = _mapping(entry.get("settings"), f"overrides[{i}] {target}", errors)
        if not target:
            errors.append(f"overrides[{i}]: name a premise, or premise/device")
            continue
        if not settings:
            errors.append(f"overrides[{i}] {target}: set at least one setting")
            continue
        part = "device" if "/" in target else "premise"
        overrides.setdefault(target, {}).setdefault(part, {}).update(settings)
    additions = []
    for i, entry in enumerate(form.get("add_devices") or []):
        where = _mapping(entry.get("where"), f"add_devices[{i}] where", errors)
        device = _mapping(entry.get("device"), f"add_devices[{i}] device", errors)
        item = ({"where": where} if where else {})
        try:
            item |= {"share": float(yaml.safe_load(str(entry.get("share") or "")) or 0),
                     "seed": int(yaml.safe_load(str(entry.get("seed") or "0")) or 0)}
        except (TypeError, ValueError, yaml.YAMLError):
            errors.append(f"add_devices[{i}]: share is a number from 0 to 1, and seed a whole number")
            continue
        item["device"] = device
        try:
            AddDevicesConfig.model_validate(item)
        except ValidationError as exc:
            errors.append(f"add_devices[{i}]: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'entry'}: {e['msg']}" for e in exc.errors()))
        additions.append(item)
    if errors:
        raise FormErrors(errors)
    return {"defaults": defaults, "overrides": overrides, "add_devices": additions}


def apply_form(text: str, form: dict) -> str:
    """The file's text with the three sections rewritten from the forms."""
    return replace_sections(text, sections_from_form(form))
