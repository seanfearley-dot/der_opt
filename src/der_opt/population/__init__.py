"""The population model (docs/POPULATION_MODEL_SPEC.md): one resolved premise that every
way of describing a premise compiles to, the precedence rule, and device templates."""

from der_opt.population.compile import PopulationError, resolve_feeder, resolve_scenario
from der_opt.population.convert import convert_premises
from der_opt.population.precedence import LEVELS, Layer, resolve
from der_opt.population.report import parameters_table, population_problems, premises_table
from der_opt.population.resolved import ResolvedDevice, ResolvedPremise
from der_opt.population.templates import Template, load_template

__all__ = [
    "LEVELS",
    "Layer",
    "PopulationError",
    "ResolvedDevice",
    "ResolvedPremise",
    "Template",
    "convert_premises",
    "load_template",
    "parameters_table",
    "population_problems",
    "premises_table",
    "resolve",
    "resolve_feeder",
    "resolve_scenario",
]
