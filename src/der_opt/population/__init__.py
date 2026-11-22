"""The population model (docs/POPULATION_MODEL_SPEC.md): one resolved premise that every
way of describing a premise compiles to, the precedence rule, and device templates."""

from der_opt.population.compile import resolve_feeder, resolve_scenario
from der_opt.population.precedence import LEVELS, Layer, resolve
from der_opt.population.resolved import ResolvedDevice, ResolvedPremise
from der_opt.population.templates import Template, load_template

__all__ = [
    "LEVELS",
    "Layer",
    "ResolvedDevice",
    "ResolvedPremise",
    "Template",
    "load_template",
    "resolve",
    "resolve_feeder",
    "resolve_scenario",
]
