"""The portfolio analysis (docs/POPULATION_MODEL_SPEC.md sections 4 and 4a): each
selected premise on its own, with the study's DR program, screening values and cost
tests, as a single-home scenario would run them; then a summary across premises.

Per premise:
  - the measure is the devices the `measure` selector picks (default: all its devices);
    DR's R3 removes them;
  - the cost tests compare against the home `cost_test.without` describes (by device
    name or type; cost test spec v1.3); participant costs are the study's base, plus the
    installed_cost and annual_om of the devices that comparison removes, plus the
    premise's own participant_upfront / participant_annual_om;
  - customer_class comes from the premise (DR settlement: 5-in-10 or 10-in-10).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from der_opt.population.compile import matches, resolve_study


@dataclass
class PortfolioPremise:
    premise_id: str
    config: object | None  # the premise's single-home ScenarioConfig, or None if it's skipped
    measure: list[str] = field(default_factory=list)
    note: str = ""


def _row(resolved) -> dict:
    """What a `select` can test, for a resolved premise."""
    return {**{k: v for k, v in resolved.extra.items()}, **{k: v for k, v in resolved.settings.items()
                                                            if isinstance(v, (str, int, float, bool))},
            "element_id": resolved.element_id or ""}


def portfolio_premises(study, resolved=None) -> tuple[list[PortfolioPremise], list[str]]:
    """Each selected premise's scenario, and the problems that stop one being built."""
    from der_opt.scenarios.schema import ScenarioConfig

    analysis = study.analyses.portfolio
    if resolved is None:
        resolved, _, _ = resolve_study(study)
    out, problems = [], []
    for r in resolved:
        row = _row(r)
        if not matches(analysis.select, r.premise_id, row):
            continue
        devices = [d for d in r.devices
                   if analysis.measure is None or matches(analysis.measure, r.premise_id, row,
                                                          {"device_id": d.device_id, "type": d.kind,
                                                           "template": d.template or ""})]
        measure = [d.device_id for d in devices]
        if analysis.measure is not None and not measure and (analysis.dr is not None or analysis.cost_test is not None):
            out.append(PortfolioPremise(r.premise_id, None, [], "no devices match the measure; skipped"))
            continue
        data = r.to_scenario_config(study.time_grid).model_dump(mode="json", exclude_none=True)
        data["weather_case"] = study.weather_case
        if analysis.dr is not None:
            customer_class = r.settings.get("customer_class")
            if not customer_class:
                problems.append(f"{r.premise_id}: DR needs its customer_class (residential or commercial)")
                continue
            data["dr"] = {**analysis.dr, "customer_class": customer_class, "measure_devices": measure}
        if analysis.avoided_costs is not None:
            data["avoided_costs"] = analysis.avoided_costs.model_dump()
        if analysis.cost_test is not None:
            from der_opt.cost_test.without import WithoutError, device_modes

            try:  # the devices the comparison home removes are bought by the measure (cost test spec v1.3)
                modes, _ = device_modes([(d.device_id, d.kind) for d in r.devices], analysis.cost_test.get("without")
                                        or {})
            except WithoutError as exc:
                problems.append(f"{r.premise_id}: {exc}")
                continue
            bought = [d for d in r.devices if modes[d.device_id]["mode"] == "removed"]
            base = dict(analysis.cost_test.get("participant_costs") or {})
            upfront = float(base.get("upfront", 0.0)) + sum(d.costs.get("installed_cost", 0.0) for d in bought)
            upfront += float(r.settings.get("participant_upfront") or 0.0)
            annual = float(base.get("annual_om", 0.0)) + sum(d.costs.get("annual_om", 0.0) for d in bought)
            annual += float(r.settings.get("participant_annual_om") or 0.0)
            data["cost_test"] = {**analysis.cost_test,
                                 "participant_costs": {**base, "upfront": upfront, "annual_om": annual}}
        try:
            config = ScenarioConfig.model_validate(data)
        except ValueError as exc:
            problems.append(f"{r.premise_id}: {exc}")
            continue
        out.append(PortfolioPremise(r.premise_id, config, measure))
    if not out and not problems:
        problems.append("portfolio: the select picks no premises")
    return out, problems


def _counted(folder: Path) -> dict:
    path = folder / "dr_capacity.csv"
    if not path.exists():
        return {}
    capacity = pd.read_csv(path)
    season = capacity[capacity["in_season"]] if "in_season" in capacity else capacity
    out = {}
    for column in capacity.columns:
        if column.startswith("counted_") and column.endswith("_kw") and not column.endswith(("_min_kw", "_max_kw")):
            out[f"{column[:-3]}_season_mean_kw"] = round(float(season[column].mean()), 3)
            out[f"{column[:-3]}_peak_month_kw"] = round(float(capacity[column].max()), 3)
    return out


def summarize(folder: Path, premises: list[PortfolioPremise]) -> pd.DataFrame:
    """One row per premise from its run's outputs, and a total row (sums; ratios aren't
    summed)."""
    rows = []
    for item in premises:
        row = {"premise_id": item.premise_id, "measure": "; ".join(item.measure), "note": item.note}
        run = folder / item.premise_id
        record = run / "run_record.json"
        if item.config is None or not record.exists():
            rows.append(row)
            continue
        results = json.loads(record.read_text(encoding="utf-8"))["results"]
        row["optimal_cost"] = round(sum((results.get("optimal") or {}).values()), 2)
        if results.get("counterfactual"):
            row["counterfactual_cost"] = round(sum(results["counterfactual"].values()), 2)
            row["value_of_optimization"] = round(row["counterfactual_cost"] - row["optimal_cost"], 2)
        for label, bill in (results.get("bills") or {}).items():
            row[f"bill_{label}"] = round(bill, 2)
        dr = results.get("dr") or {}
        if dr.get("incentive_earned") is not None:
            row["dr_incentive_earned"] = round(dr["incentive_earned"], 2)
        row |= _counted(run)
        for key, value in (results.get("screening") or {}).items():
            row[f"screening {key}"] = value
        summary_path = run / "summary.json"
        cases = json.loads(summary_path.read_text(encoding="utf-8")).get("cases") if summary_path.exists() else None
        for name, case in (cases or {}).items():  # each comparison case's bill and total cost
            row[f"case_bill_{name}"] = round(case["bill"]["net_cost"], 2) if case.get("bill") else None
            row[f"case_cost_{name}"] = round(case["costs"]["total"], 2)
        tests = run / "openbca" / "cost_tests.csv"
        if tests.exists():
            for _, t in pd.read_csv(tests).iterrows():
                row[f"{t['test']}_ratio"] = round(float(t["openbca_ratio"]), 3)
                row[f"{t['test']}_net_benefits"] = round(float(t["net_benefits"]), 2)
        rows.append(row)
    table = pd.DataFrame(rows)
    numeric = [c for c in table.columns if c not in ("premise_id", "measure", "note") and not c.endswith("_ratio")
               and pd.api.types.is_numeric_dtype(table[c])]
    total = {"premise_id": "total", **{c: round(float(table[c].sum()), 2) for c in numeric}}
    return pd.concat([table, pd.DataFrame([total])], ignore_index=True)
