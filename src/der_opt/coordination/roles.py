"""Who takes part in coordination, and how (docs/COORDINATION_SPEC.md sections 1-1a):
each customer's participation, baseline behavior and response, and its rules with the
defaults merged in."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from der_opt.devices import (
    BatteryStorage,
    CurtailableLoad,
    SmartThermostat,
    SolarPV,
    V2XVehicle,
)

MECHANISM_RULES = ("dr_event", "price_signal", "envelope")

# section 1a's defaults; a house's rules (and house_defaults.rules) merge over these
DEFAULT_RULES: dict[str, dict[str, dict]] = {
    "dr_event": {
        "ev": {"action": "pause", "catch_up": True},
        "battery": {"action": "discharge", "to": "home_load", "precharge_hours": 0},
        "thermostat": {"action": "setback", "offset_c": 2.0, "precool_hours": 0, "precool_offset_c": 1.0},
        "flexible_load": {"action": "shed", "blocks": "all"},
    },
    "price_signal": {
        "ev": {"action": "pause", "above_per_kwh": 0.20},
        "battery": {"action": "discharge", "to": "home_load", "above_per_kwh": 0.20},
        "thermostat": {"action": "setback", "offset_c": 2.0, "above_per_kwh": 0.20},
        "flexible_load": {"action": "shed", "blocks": "all", "above_per_kwh": 0.20},
    },
    "envelope": {
        "ev": {"action": "limit"},
        "battery": {"action": "limit"},
        "solar": {"action": "curtail"},
        "flexible_load": {"action": "shed"},
    },
}

# actions each device type accepts, and their settings
ACTIONS: dict[str, dict[str, set[str]]] = {
    "ev": {"pause": {"catch_up", "above_per_kwh"}, "discharge": {"to", "max_kw", "keep_soc_frac", "above_per_kwh"},
           "limit": set(), "none": {"above_per_kwh"}},
    "battery": {"discharge": {"to", "max_kw", "keep_soc_frac", "precharge_hours", "above_per_kwh"},
                "limit": set(), "none": {"above_per_kwh"}},
    "thermostat": {"setback": {"offset_c", "precool_hours", "precool_offset_c", "above_per_kwh"},
                   "none": {"above_per_kwh"}},
    "flexible_load": {"shed": {"blocks", "above_per_kwh"}, "none": {"above_per_kwh"}},
    "solar": {"curtail": set(), "none": set()},
}


def device_kind(device) -> str | None:
    if isinstance(device, V2XVehicle):
        return "ev"
    if isinstance(device, BatteryStorage):
        return "battery"
    if isinstance(device, SmartThermostat):
        return "thermostat"
    if isinstance(device, CurtailableLoad):
        return "flexible_load"
    if isinstance(device, SolarPV):
        return "solar"
    return None


def merge_rules(*layers: dict) -> dict:
    out: dict = copy.deepcopy(layers[0]) if layers else {}
    for layer in layers[1:]:
        for mechanism, devices in (layer or {}).items():
            for key, rule in (devices or {}).items():
                if rule is None:
                    out.setdefault(mechanism, {}).pop(key, None)
                    continue
                current = out.setdefault(mechanism, {}).get(key, {})
                # a new action replaces the old one's settings; the same action merges
                base = current if rule.get("action", current.get("action")) == current.get("action") else {}
                out[mechanism][key] = {**base, **rule}
    return out


@dataclass
class Role:
    participation: str = "participant"
    baseline_behavior: str = "own_bill"
    response: str = "optimized"
    rules: dict = field(default_factory=dict)  # merged: defaults, house_defaults, the house's own
    weight: float | None = None  # envelopes, method weights

    @property
    def participant(self) -> bool:
        return self.participation == "participant"

    @property
    def group(self) -> str:
        if not self.participant:
            return "non-participants"
        return "participants, rules" if self.response == "rules" else "participants, optimized"

    def rule_for(self, mechanism: str, device) -> dict | None:
        """The rule a device follows for a mechanism: by its name, else by its type."""
        rules = self.rules.get(mechanism, {})
        rule = rules.get(device.name) or rules.get(device_kind(device) or "")
        if rule is None or rule.get("action") == "none":
            return None
        return rule


def house_role(defaults, house) -> Role:
    pick = lambda attr: getattr(house, attr, None) if getattr(house, attr, None) is not None else getattr(defaults, attr)
    return Role(
        participation=pick("participation"),
        baseline_behavior=pick("baseline_behavior"),
        response=pick("response"),
        rules=merge_rules(DEFAULT_RULES, defaults.rules, getattr(house, "rules", {}) or {}),
        weight=getattr(house, "envelope_weight", None),
    )


def rule_problems(name: str, role: Role, premise, explicit: dict) -> list[str]:
    """What's wrong with a house's rules: actions that don't fit its devices, settings
    an action doesn't take, rules for devices it doesn't have (when written on the
    house itself), rules that can't apply."""
    problems = []
    if role.response == "rules" and role.baseline_behavior != "rule_based":
        problems.append(f"{name}: response: rules keeps the baseline rules, so it needs baseline_behavior: rule_based")
    devices = {d.name: d for d in premise.devices}
    kinds = {device_kind(d) for d in premise.devices}
    for mechanism, rules in role.rules.items():
        if mechanism not in MECHANISM_RULES:
            problems.append(f"{name}: rules.{mechanism} isn't a mechanism ({', '.join(MECHANISM_RULES)})")
            continue
        for key, rule in rules.items():
            kind = device_kind(devices[key]) if key in devices else key
            if kind not in ACTIONS:
                if key in (explicit.get(mechanism) or {}):
                    problems.append(f"{name}: rules.{mechanism}.{key} isn't a device type or one of its devices")
                continue
            if kind not in kinds and key not in devices:
                if key in (explicit.get(mechanism) or {}):
                    problems.append(f"{name}: rules.{mechanism}.{key}: it has no {key}")
                continue
            action = rule.get("action")
            if action not in ACTIONS[kind]:
                problems.append(f"{name}: rules.{mechanism}.{key}: {action!r} isn't an action for a {kind} "
                                f"({', '.join(ACTIONS[kind])})")
                continue
            unknown = set(rule) - {"action"} - ACTIONS[kind][action]
            if unknown:
                problems.append(f"{name}: rules.{mechanism}.{key}: {action} doesn't take {sorted(unknown)}")
            if mechanism == "price_signal" and action != "none" and "above_per_kwh" not in rule:
                problems.append(f"{name}: rules.price_signal.{key} needs above_per_kwh")
            if action == "discharge" and kind == "ev":
                evs = [d for d in premise.devices if device_kind(d) == "ev" and (key == "ev" or d.name == key)]
                if any(d.p_max_discharge_kw <= 0 for d in evs):
                    problems.append(f"{name}: rules.{mechanism}.{key}: discharge needs a V2X EV "
                                    "(p_max_discharge_kw above 0)")
            if rule.get("to") not in (None, "home_load", "export"):
                problems.append(f"{name}: rules.{mechanism}.{key}: to is home_load or export")
    return problems
