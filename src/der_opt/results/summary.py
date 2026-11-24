"""summary.json: the numbers a run prints, as data (docs/REPORTS.md section 1).

Every output folder gets one: a single-home scenario, a feeder, and a study (whose
summary points to its feeder folder's and each portfolio premise's). Reports and the
app read these, never the printed text. `kind` says which shape it is;
`summary_version` changes when a shape changes in a way readers must know about.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

SUMMARY_VERSION = "1.1"  # 1.1 adds cases and case_compare (docs/COMPARISON_CASES_SPEC.md)
FILE = "summary.json"


def jsonable(value):
    """Plain JSON values: numpy and pandas scalars, timestamps as ISO text, NaN as null."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, pd.DataFrame):
        return jsonable(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return jsonable(value.to_dict())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if value is pd.NaT:
        return None
    return value


def _time_grid(time_grid) -> dict:
    stamps = time_grid.timestamps()
    return {"start": stamps[0], "end": stamps[-1] + pd.Timedelta(minutes=time_grid.resolution_minutes),
            "resolution_minutes": time_grid.resolution_minutes, "steps": time_grid.horizon_steps}


def _checks(report) -> dict:
    return {"errors": len(report.errors), "warnings": len(report.warnings),
            "findings": [{"level": f.level, "check": f.check, "message": f.message, "run": f.run, "premise": f.premise}
                         for f in report.findings if f.level != "info"]}


def _base(kind: str, name: str, time_grid, report) -> dict:
    return {"summary_version": SUMMARY_VERSION, "kind": kind, "name": name,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "time_grid": _time_grid(time_grid) if time_grid is not None else None,
            "checks": _checks(report) if report is not None else None}


def write_summary(folder: str | Path, summary: dict) -> Path:
    folder = Path(folder)
    summary["files"] = sorted(str(p.relative_to(folder)) for p in folder.iterdir() if p.name != FILE)
    path = folder / FILE
    path.write_text(json.dumps(jsonable(summary), indent=1), encoding="utf-8")
    return path


def read_summary(folder: str | Path) -> dict | None:
    path = Path(folder) / FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def update_summary(folder: str | Path, **sections) -> None:
    """Adds sections to a folder's summary (cost tests, which run after it's written)."""
    summary = read_summary(folder)
    if summary is None:
        return
    summary.update(jsonable(sections))
    write_summary(folder, summary)


# --- a single-home scenario ----------------------------------------------------------


def scenario_summary(config, premise, dispatch, breakdown: dict, counterfactual=None, cf_breakdown: dict | None = None,
                     bills: dict | None = None, dr_study=None, screening=None, screening_info: dict | None = None,
                     ev_patterns: list | None = None, report=None, cases: dict | None = None,
                     compare: tuple | None = None) -> dict:
    out = _base("scenario", premise.name, premise.time_grid, report)
    out["status"] = str(dispatch.solver_status)
    out["devices"] = [{"name": d.name, "type": d.type, "template": d.template} for d in config.devices]
    out["ev_patterns"] = ev_patterns or []
    costs = {"optimal": {**breakdown, "total": sum(breakdown.values())}}
    if cf_breakdown is not None:
        costs["counterfactual"] = {**cf_breakdown, "total": sum(cf_breakdown.values())}
        costs["value_of_optimization"] = sum(cf_breakdown.values()) - sum(breakdown.values())
    out["costs"] = costs
    out["counterfactual_policies"] = (
        {name: policy.get("type") for name, policy in config.counterfactual.policies.items()}
        if config.counterfactual is not None and counterfactual is not None else None)
    out["bills"] = {label: bill.summary for label, bill in (bills or {}).items()}
    if dr_study is not None:
        capacity = dr_study.capacity
        keep = [c for c in capacity.columns if not c.endswith(("_min_kw", "_max_kw"))]
        out["dr"] = {**dr_study.summary, "capacity_by_month": capacity[keep]}
    if screening is not None:
        values: dict = {}
        for (category, part), value in screening.groupby(["category", "part"])["value"].sum().items():
            values.setdefault(category, {})[part] = round(float(value), 2)
        out["screening"] = {**(screening_info or {}), "values": values}
    if cases:
        from der_opt.cases import buttons, case_summary, cost_test_pair

        dt = premise.time_grid.dt_hours
        names = list(cases)
        out["cases"] = {name: case_summary(case, dt) for name, case in cases.items()}
        tested = cost_test_pair(config, names)
        out["case_compare"] = {"without": compare[0], "with": compare[1], "why": compare[2],
                               "cost_tests": list(tested) if tested else None, "buttons": buttons(config, names)}
    return out


# --- a feeder -----------------------------------------------------------------------


def feeder_summary(built, study, loading: dict, report=None) -> dict:
    runs = study.runs
    time_grid = built.premises[0].time_grid
    out = _base("feeder", built.config.name, time_grid, report)
    houses = built.houses
    out["premises"] = len(built.premises)
    out["elements"] = [{"name": e.name, "rating_kw": e.rating_kw, "parent": e.parent,
                        "overload_cost_per_kwh": e.overload_cost_per_kwh, "emergency_rating_kw": e.emergency_rating_kw,
                        "violation_cost_per_kwh": e.violation_cost_per_kwh,
                        "emergency_kwh_per_day": e.emergency_kwh_per_day} for e in built.feeder.elements]
    mix = {}
    for element, group in houses.groupby("element"):
        devices = [d.split(", ") for d in group["devices"]]
        mix[element] = {"premises": len(group), **{k: sum(k in d for d in devices) for k in ("solar", "battery", "ev")}}
    out["mix"] = mix
    out["background"] = list(built.background)
    from der_opt.scenarios.feeder import shortfall_kwh

    out["runs"] = {label: {"total": run.total, "customer_bills": float(run.house_costs["bill"].sum()),
                           "degradation": float(run.house_costs["degradation"].sum()),
                           "ev_shortfall": float(run.house_costs["ev_shortfall"].sum()),
                           "ev_shortfall_kwh": float(shortfall_kwh(built.premises, run.dispatches).sum()),
                           "network_costs": run.network_costs} for label, run in runs.items()}
    out["loading"] = {label: frame.drop(columns=["element"], errors="ignore").to_dict(orient="index")
                      for label, frame in loading.items()}
    prices = study.congestion_prices
    congestion = {}
    for element in built.feeder.elements:
        series = prices[element.name]
        binding = series[series.abs() > 1e-3]
        congestion[element.name] = None if binding.empty else {
            "max_per_kwh": float(binding.max()), "hours": len(binding), "highest_at": binding.idxmax(),
            "per_kw_over_run": float(binding.sum() * time_grid.dt_hours)}
    out["congestion"] = {"exact": bool(study.prices_exact), "elements": congestion}
    if "independent" in runs:
        from der_opt.scenarios.feeder import customer_cost

        change = customer_cost(runs["coordinated"].house_costs) - customer_cost(runs["independent"].house_costs)
        worse = change[change > 0.005]
        out["coordinated_vs_independent"] = {
            "premises_paying_more": len(worse), "of": len(change), "total_paid_more": float(worse.sum()),
            "most": float(worse.max()) if len(worse) else 0.0, "most_by": worse.idxmax() if len(worse) else None,
            "network_saving": sum(runs["independent"].network_costs.values())
            - sum(runs["coordinated"].network_costs.values())}
    if study.coordination is not None:
        table = study.coordination_table()
        out["coordination"] = {
            "groups": houses["group"].value_counts().to_dict(),
            "runs": {label: {**table.loc[label].to_dict(), "mechanisms": list(run.mechanisms),
                             "events": len(run.calendar) if run.calendar is not None else None}
                     for label, run in study.coordination.runs.items()}}
    return out


# --- a study -------------------------------------------------------------------------


def study_summary(study, resolved, additions, portfolio=None, failed=None, table=None, report=None) -> dict:
    from der_opt.scenarios.loader import _build_time_grid

    out = _base("study", study.name, _build_time_grid(study.time_grid), report)
    out["premises"] = len(resolved)
    by_type: dict = {}
    by_element: dict = {}
    for r in resolved:
        kinds = {d.kind for d in r.devices}
        for kind in kinds:
            by_type[kind] = by_type.get(kind, 0) + 1
        if r.element_id:
            by_element[r.element_id] = by_element.get(r.element_id, 0) + 1
    out["population"] = {"premises_with": by_type, "by_element": by_element,
                         "origins": pd.Series([r.origin for r in resolved]).value_counts().to_dict()}
    out["add_devices"] = additions or []
    out["analyses"] = [k for k in ("feeder", "portfolio") if getattr(study.analyses, k) is not None]
    if study.analyses.feeder is not None:
        out["feeder"] = {"folder": "feeder"}
    if portfolio is not None:
        out["portfolio"] = {
            "folder": "portfolio", "table": "portfolio/portfolio.csv",
            "premises": [{"premise_id": p.premise_id, "measure": p.measure, "note": p.note,
                          "folder": f"portfolio/{p.premise_id}" if p.config is not None else None,
                          "failed": p.premise_id in (failed or [])} for p in portfolio],
            "totals": table.iloc[-1].drop(labels=["premise_id", "measure", "note"], errors="ignore").to_dict()
            if table is not None else None}
    return out
