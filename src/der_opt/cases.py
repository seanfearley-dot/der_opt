"""Comparison cases (docs/COMPARISON_CASES_SPEC.md v1.0): the same home, load and
weather over the same window, with different devices present or run differently, each
priced the same way, so the report can compare any two.

  no_devices   the home's load alone (nothing to decide, so it's metered, not solved)
  no_measure   every device except the measure's, optimized (DR's R3)
  rule_based   every device, run by the counterfactual policies (R1)
  optimized    every device, optimized, no DR events (R2)
  responding   every device, optimized, responding to DR events

The measure's devices are cost_test.measure_devices, then dr.measure_devices, then
every device: the rules R3 and the cost tests use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from der_opt.results.schema import DispatchResult

CASES = ("no_devices", "no_measure", "rule_based", "optimized", "responding")
# the study page's names: a premise's own labels depend on its devices
GENERIC_LABELS = {"no_devices": "No devices", "no_measure": "Without the measure", "rule_based": "Rule-based",
                  "optimized": "Optimized", "responding": "Optimized + DR"}
WORDS = {"solar": "solar", "battery": "battery", "ev_v2x": "EV", "thermostat": "thermostat",
         "curtailable_load": "curtailable load"}


class CasesError(ValueError):
    """A `cases` section asks for a case this run can't make."""


@dataclass
class Case:
    name: str
    label: str
    description: str
    devices: list[str]
    result: DispatchResult
    premise: object  # the premise that prices it: its devices and value streams
    policies: dict | None = None
    costs: dict = field(default_factory=dict)
    bill: object | None = None
    tariff: str | None = None
    program: str | None = None
    dr_incentive: float | None = None


# --- which cases, and their names --------------------------------------------------


def measure_devices(config) -> list[str]:
    names = [d.name for d in config.devices]
    if config.cost_test is not None and config.cost_test.measure_devices is not None:
        return list(config.cost_test.measure_devices)
    if config.dr is not None and config.dr.measure_devices is not None:
        return list(config.dr.measure_devices)
    return names


def unavailable(config, counterfactual: bool = True) -> dict[str, str]:
    """Each case this run can't make, and why."""
    out = {}
    if not config.devices:
        out["no_devices"] = "the home has no devices, so it's the optimized case"
        out["no_measure"] = "the home has no devices"
    elif set(measure_devices(config)) >= {d.name for d in config.devices}:
        out["no_measure"] = "the measure is every device, so it would be no_devices"
    if config.counterfactual is None:
        out["rule_based"] = "the scenario has no counterfactual section"
    elif not counterfactual:
        out["rule_based"] = "the run was told not to simulate the counterfactual"
    if config.dr is None:
        out["responding"] = "the scenario has no dr section"
    return out


def planned(config, counterfactual: bool = True) -> list[str]:
    """The cases to make, in order. Raises CasesError if `cases.include` names one the
    run can't make."""
    missing = unavailable(config, counterfactual)
    settings = config.cases
    if settings is not None and settings.include is not None:
        wrong = [name for name in settings.include if name in missing]
        if wrong:
            raise CasesError("; ".join(f"cases.include names {name}, but {missing[name]}" for name in wrong))
        return [name for name in CASES if name in settings.include]
    return [name for name in CASES if name not in missing]


def _words(devices) -> list[str]:
    words = [WORDS.get(d.type, d.type.replace("_", " ")) for d in devices]
    return [d.name if words.count(w) > 1 else w for d, w in zip(devices, words, strict=True)]


def _join(words: list[str]) -> str:
    if not words:
        return "nothing"
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _first_upper(text: str) -> str:
    return text[:1].upper() + text[1:]


def labels(config) -> dict[str, tuple[str, str]]:
    """(label, description) for every case, from the scenario's devices."""
    measure = set(measure_devices(config))
    kept = [d for d in config.devices if d.name not in measure]
    removed = [d for d in config.devices if d.name in measure]
    policies = (config.counterfactual.policies if config.counterfactual is not None else {}) or {}
    ruled = [d for d in config.devices if d.name in policies]
    ev_note = lambda gone: ("; without the EV, its charging load goes too" if any(d.type == "ev_v2x" for d in gone)
                            else "")
    program = config.dr.program.program_id if config.dr is not None else ""
    return {
        "no_devices": ("No devices", "the home's load alone" + ev_note(config.devices)),
        "no_measure": (_first_upper(_join(_words(kept))) + " only",
                       f"without the {_join(_words(removed))}, optimized" + ev_note(removed)),
        "rule_based": (f"{_first_upper(_join(_words(ruled)))}, rule-based" if ruled else "Rule-based, devices passive",
                       "every device; " + (", ".join(f"{name}: {p.get('type')}" for name, p in policies.items())
                                           or "every device passive")),
        "optimized": ("Optimized", "every device optimized, no DR events"),
        "responding": ("Optimized + DR", f"every device optimized, responding to the {program} events"),
    }


def buttons(config, names: list[str]) -> list[dict]:
    """The report's quick buttons: the usual questions, where both cases exist."""
    measure = set(measure_devices(config))
    kept = [d for d in config.devices if d.name not in measure]
    removed = [d for d in config.devices if d.name in measure]
    out = [
        (_first_upper(_join(_words(kept))), "no_devices", "no_measure"),
        (f"The {_join(_words(removed))}, rule-based", "no_measure", "rule_based"),
        ("Optimization", "rule_based", "optimized"),
        ("DR participation", "optimized", "responding"),
        ("Everything", "no_devices", "responding" if "responding" in names else "optimized"),
    ]
    return [{"label": label, "without": a, "with": b} for label, a, b in out if a in names and b in names]


def cost_test_pair(config, names: list[str]) -> tuple[str, str] | None:
    """The two cases the cost tests compare, when both are cases."""
    if config.cost_test is None:
        return None
    with_ = "responding" if config.dr is not None else "optimized"
    without = "rule_based" if config.cost_test.scope == "control" else "no_measure"
    return (without, with_) if without in names and with_ in names else None


def opening_pair(config, names: list[str]) -> tuple[str, str, str]:
    """(without, with, why) for the report's first view."""
    settings = config.cases
    if settings is not None and settings.compare is not None:
        pair = (settings.compare.without, settings.compare.with_)
        wrong = [name for name in pair if name not in names]
        if wrong:
            raise CasesError(f"cases.compare names {', '.join(wrong)}, which this run doesn't make "
                             f"(it makes {', '.join(names)})")
        return (*pair, "cases.compare")
    pair = cost_test_pair(config, names)
    if pair is not None:
        return (*pair, "the cost tests' comparison")
    if "rule_based" in names and "optimized" in names:
        return "rule_based", "optimized", "the value of optimization"
    return names[0], "optimized", "the first case against the optimized one"


# --- making and pricing them -------------------------------------------------------


def case_billing(config, name: str, run_billing):
    """(tariff, program, kwargs, program type) for a case: its own billing override, or the run's."""
    from der_opt.scenarios.loader import build_billing
    from der_opt.scenarios.schema import BillingConfig

    override = getattr(config.cases, name, None) if config.cases is not None else None
    if override is None or override.billing is None:
        kind = (config.billing.program or {}).get("type") if config.billing is not None else None
        return (*run_billing, kind) if run_billing is not None else None
    billing = override.billing
    if isinstance(billing, str):
        from der_opt.scenarios.compose import compose_scenario

        data = compose_scenario(billing).data
        if "billing" not in data:
            raise CasesError(f"cases.{name}.billing: {billing} has no billing section")
        billing = BillingConfig.model_validate(data["billing"])
    built = build_billing(config.model_copy(update={"billing": billing}))
    return (*built, (billing.program or {}).get("type"))


def _dr_incentive(dr_study) -> float | None:
    performance = dr_study.performance
    if "incentive_earned" not in performance or not len(performance):
        return None
    return float(performance.loc[performance["replicate"] == 0, "incentive_earned"].sum())


def build_cases(config, premise, dispatch: DispatchResult, counterfactual: DispatchResult | None = None,
                dr_study=None, names: list[str] | None = None) -> dict[str, Case]:
    """Every planned case, from the run's own results where it has them (the main solve,
    the counterfactual, the DR study's R3 and responding run), priced alike."""
    from der_opt.cost_test.impacts import config_without_devices
    from der_opt.pricing import value_breakdown
    from der_opt.scenarios.loader import build_billing, build_premise
    from der_opt.simulation import CounterfactualSimulator

    names = names if names is not None else planned(config, counterfactual is not None)
    named = labels(config)
    measure = measure_devices(config)
    all_names = [d.name for d in config.devices]
    run_billing = build_billing(config)
    out: dict[str, Case] = {}
    for name in names:
        policies = None
        if name == "optimized":
            case_premise, result, devices = premise, dispatch, all_names
        elif name == "rule_based":
            if counterfactual is None:
                raise CasesError("rule_based needs the counterfactual simulation")
            case_premise, result, devices = premise, counterfactual, all_names
            policies = {n: p.get("type") for n, p in config.counterfactual.policies.items()}
        elif name == "responding":
            if dr_study is None:
                raise CasesError("responding needs the DR study")
            case_premise, result, devices = premise, dr_study.responding[0], all_names  # priced without the DR stream
        elif name == "no_measure":
            reduced = config_without_devices(config, measure).model_copy(update={"cases": None})
            case_premise = build_premise(reduced)
            dr_measure = set(config.dr.measure_devices or all_names) if config.dr is not None else None
            if dr_study is not None and dr_measure == set(measure):
                result = dr_study.references["R3"]
            else:
                result = case_premise.solve()[0]
            devices = [d.name for d in reduced.devices]
        else:  # no_devices: nothing to decide, so the meter reads the load
            reduced = config_without_devices(config, None).model_copy(update={"cases": None})
            case_premise = build_premise(reduced)
            result = CounterfactualSimulator(case_premise, {}).run()
            devices = []
        label, description = named[name]
        case = Case(name, label, description, devices, result, case_premise, policies)
        breakdown = value_breakdown(case_premise, result)
        case.costs = {**breakdown, "total": sum(breakdown.values())}
        billing = case_billing(config, name, run_billing)
        if billing is not None:
            from der_opt.billing import bill_from_dispatch

            tariff, program, kwargs, kind = billing
            case.bill = bill_from_dispatch(result, premise.time_grid, tariff, program, **kwargs)
            case.tariff, case.program = tariff.name, kind
            if run_billing is not None and tariff.name != run_billing[0].name:
                case.label += f" ({tariff.name})"
        if name == "responding":
            case.dr_incentive = _dr_incentive(dr_study)
        out[name] = case
    return out


# --- what's written ----------------------------------------------------------------


def _solar_kw(case: Case) -> pd.Series | None:
    from der_opt.devices.solar import SolarPV

    columns = [f"{d.name}.p_gen" for d in case.premise.devices if isinstance(d, SolarPV)]
    columns = [c for c in columns if c in case.result.device_dispatch]
    if not columns:
        return None
    return case.result.device_dispatch[columns].sum(axis=1)


def energy(case: Case, dt: float) -> dict:
    imported, exported = case.result.grid_import, case.result.grid_export
    out = {"import_kwh": float(imported.sum() * dt), "export_kwh": float(exported.sum() * dt),
           "peak_import_kw": float(imported.max()), "peak_import_at": imported.idxmax()}
    solar = _solar_kw(case)
    if solar is not None:
        out["solar_kwh"] = float(solar.sum() * dt)
        # solar not exported, step by step
        out["self_consumed_solar_kwh"] = float(np.maximum(solar.to_numpy() - exported.to_numpy(), 0).sum() * dt)
    return out


def _cycle_starts(cycles: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(cycles.get("cycle_start", cycles.index)))


def by_month(case: Case, dt: float) -> list[dict]:
    imported, exported = case.result.grid_import, case.result.grid_export
    index = pd.DatetimeIndex(imported.index)
    month = index.strftime("%Y-%m")
    frame = pd.DataFrame({"import_kwh": imported.to_numpy() * dt, "export_kwh": exported.to_numpy() * dt,
                          "peak_import_kw": imported.to_numpy()}, index=month)
    grouped = frame.groupby(level=0).agg({"import_kwh": "sum", "export_kwh": "sum", "peak_import_kw": "max"})
    if case.bill is not None:
        cycles = case.bill.cycles
        bills = pd.Series(cycles["amount_due"].to_numpy(), index=_cycle_starts(cycles).strftime("%Y-%m"))
        grouped["bill"] = bills.groupby(level=0).sum().reindex(grouped.index)
    return [{"month": m, **row} for m, row in grouped.to_dict(orient="index").items()]


def case_summary(case: Case, dt: float) -> dict:
    return {"label": case.label, "description": case.description, "devices": case.devices,
            "policies": case.policies, "tariff": case.tariff, "program": case.program, "costs": case.costs,
            "bill": case.bill.summary if case.bill is not None else None, "energy": energy(case, dt),
            "dr_incentive": case.dr_incentive, "by_month": by_month(case, dt)}


def cases_table(cases: dict[str, Case], dt: float) -> pd.DataFrame:
    """cases.csv: one row per case and billing cycle, and a whole-run row per case with
    its cost by value stream (the bill streams net over the run, so they don't split by
    cycle)."""
    rows = []
    for case in cases.values():
        imported, exported = case.result.grid_import, case.result.grid_export
        index = pd.DatetimeIndex(imported.index)
        if case.bill is not None:
            cycles = case.bill.cycles
            starts = _cycle_starts(cycles)
            ends = list(starts[1:]) + [index[-1] + (index[1] - index[0] if len(index) > 1 else pd.Timedelta(hours=1))]
            for start, end, (_, cycle) in zip(starts, ends, cycles.iterrows(), strict=True):
                rows_in = (index >= start) & (index < end)
                rows.append({"case": case.name, "label": case.label, "period": start.strftime("%Y-%m-%d"),
                             "bill": float(cycle["amount_due"]), "import_kwh": float(imported[rows_in].sum() * dt),
                             "export_kwh": float(exported[rows_in].sum() * dt),
                             "peak_import_kw": float(imported[rows_in].max()) if rows_in.any() else np.nan})
        whole = energy(case, dt)
        rows.append({"case": case.name, "label": case.label, "period": "run",
                     "bill": case.bill.summary["net_cost"] if case.bill is not None else np.nan,
                     "import_kwh": whole["import_kwh"], "export_kwh": whole["export_kwh"],
                     "peak_import_kw": whole["peak_import_kw"], "dr_incentive": case.dr_incentive,
                     **{f"cost: {k}": v for k, v in case.costs.items()}})
    return pd.DataFrame(rows)


def net_load_table(cases: dict[str, Case]) -> pd.DataFrame:
    """cases_net_load_kw.csv: net load at the meter (import - export), one column per case."""
    return pd.DataFrame({name: case.result.grid_import - case.result.grid_export for name, case in cases.items()})


def printed_rows(cases: dict[str, Case], dt: float) -> pd.DataFrame:
    rows = []
    for case in cases.values():
        e = energy(case, dt)
        rows.append({"case": case.label, "bill": case.bill.summary["net_cost"] if case.bill is not None else None,
                     "total cost": case.costs["total"], "import kWh": e["import_kwh"], "export kWh": e["export_kwh"],
                     "peak kW": e["peak_import_kw"]})
    return pd.DataFrame(rows)
