"""Feeder coordination (docs/COORDINATION_SPEC.md): who takes part, price signals,
dynamic operating envelopes, feeder DR events and rule-based responses."""

from der_opt.coordination.envelopes import (
    Inputs,
    Limit,
    compute_envelopes,
    thermal_limits,
    water_fill,
)
from der_opt.coordination.roles import DEFAULT_RULES, Role, house_role, merge_rules
from der_opt.coordination.rules import Context, RuleSimulator
from der_opt.coordination.study import (
    CoordinationResult,
    MechanismRun,
    check_coordination,
    run_coordination,
)

__all__ = [
    "DEFAULT_RULES",
    "Context",
    "CoordinationResult",
    "Inputs",
    "Limit",
    "MechanismRun",
    "Role",
    "RuleSimulator",
    "check_coordination",
    "compute_envelopes",
    "house_role",
    "merge_rules",
    "run_coordination",
    "thermal_limits",
    "water_fill",
]
