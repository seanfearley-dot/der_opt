"""The measure impact package (docs/COST_TEST_SPEC.md v1.0, section 2): what der-opt
hands to the cost tests. Its format is der-opt's own, independent of OpenBCA; the OpenBCA
export is built from it.

  measure.yaml        the measure: scope, life, costs, escalation, the avoided cost set
  energy_impact.csv   every hour of the analysis year, in standard time (a fixed UTC
                      offset, as OpenBCA requires): without - with kWh, split into
                      decrease_kwh and increase_kwh
  capacity.csv        monthly DR capacity kW: the chosen measure, and every other
  bills.csv           the analysis year's bills without and with, by billing cycle
  dr_incentives.csv   DR incentive earned, by event
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from der_opt.results.schema import DispatchResult


def net_load(result: DispatchResult) -> pd.Series:
    return result.grid_import - result.grid_export


def config_without_devices(config, devices: list[str] | None):
    """The scenario without the named devices (all, if None): the R3 case."""
    names = {d.name for d in config.devices}
    removed = set(devices) if devices is not None else names
    unknown = removed - names
    if unknown:
        raise ValueError(f"measure devices {sorted(unknown)} aren't devices in this scenario")
    kept = [d for d in config.devices if d.name not in removed]
    for device in kept:
        sources = set(device.params.get("charge_sources", [])) & removed
        if sources:
            raise ValueError(f"{device.name} charges from {sorted(sources)}, which the 'without' case removes")
    return config.model_copy(update={"devices": kept, "counterfactual": None, "dr": None, "cost_test": None,
                                     "cases": None})


def resolve_runs(config, cases: dict):
    """(without, with, description) for the cost tests (cost test spec v1.3): the
    comparison home built device by device, against the DR run (or the optimized run)."""
    from der_opt.cases import cost_test_pair

    pair = cost_test_pair(config, list(cases))
    if pair is None:
        raise ValueError("the cost tests need the comparison home and the run they measure; the cases section "
                         "leaves one out")
    without, with_ = cases[pair[0]], cases[pair[1]]
    return without.result, with_.result, f"{without.label} ({without.description})"


def standard_time_hours(year: int, tz: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex, str]:
    """(standard-time hours of the year as naive labels, the local prevailing-clock label
    of each, the fixed offset string). Each standard hour is one real instant."""
    offset = pd.Timestamp(f"{year}-01-15", tz=tz).utcoffset()
    standard = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", inclusive="left")
    prevailing = (standard - offset).tz_localize("UTC").tz_convert(tz).tz_localize(None)
    sign = "-" if offset < pd.Timedelta(0) else "+"
    minutes = int(abs(offset) / pd.Timedelta(minutes=1))
    return standard, prevailing, f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def energy_impact(without: DispatchResult, with_: DispatchResult, time_grid, tz: str) -> pd.DataFrame:
    """Hourly kWh impact for the analysis year in standard time. Needs a run covering
    the whole calendar year."""
    index = pd.DatetimeIndex(time_grid.timestamps())
    year = int(index[0].year)
    kwh = (net_load(without) - net_load(with_)) * time_grid.dt_hours
    kwh.index = index
    hourly = kwh.groupby(index.floor("h")).sum()
    standard, prevailing, offset = standard_time_hours(year, tz)
    values = hourly.reindex(prevailing)
    if values.isna().any():
        raise ValueError(f"the cost tests need a run covering all of {year} (the run covers {index[0]} to {index[-1]})")
    v = values.to_numpy()
    return pd.DataFrame({
        "timestamp": [f"{t.isoformat()}{offset}" for t in standard],
        "load_reduction_kwh": v, "decrease_kwh": np.clip(v, 0, None), "increase_kwh": np.clip(-v, 0, None),
    })


def write_impact_package(out_dir: Path, config, without_label: str, without: DispatchResult, with_: DispatchResult,
                         time_grid, dr_study=None, bills: dict | None = None, tz: str = "America/Los_Angeles",
                         incentives: tuple[float | None, float | None] | None = None,
                         without_modes: dict | None = None) -> Path:
    """`incentives` = (the comparison home's DR incentive, the measured run's): the
    measure's first-year incentive is the difference (cost test spec v1.3)."""
    from der_opt.avoided_costs.screening import capacity_measures

    ct = config.cost_test
    package = Path(out_dir) / "measure_impacts"
    package.mkdir(parents=True, exist_ok=True)
    energy = energy_impact(without, with_, time_grid, tz)
    energy.to_csv(package / "energy_impact.csv", index=False, float_format="%.6f")

    measures = capacity_measures(dr_study.capacity) if dr_study is not None else {}
    chosen = ct.capacity_value.label
    if dr_study is not None and chosen not in measures:
        raise ValueError(f"capacity_value {chosen} isn't in the DR results; the program's capacity_reference "
                         f"must include {ct.capacity_value.reference}")
    capacity = pd.DataFrame({"month": range(1, 13)})
    if measures:
        capacity["valued_kw"] = measures[chosen].reindex(range(1, 13)).fillna(0.0).to_numpy()
        for label, series in measures.items():
            capacity[label] = series.reindex(range(1, 13)).fillna(0.0).to_numpy()
    else:
        capacity["valued_kw"] = 0.0
    capacity.to_csv(package / "capacity.csv", index=False, float_format="%.6f")

    if bills:
        frames = []
        for label, bill in bills.items():
            frame = bill.cycles.copy()
            frame.insert(0, "case", label)
            frames.append(frame)
        pd.concat(frames).to_csv(package / "bills.csv")
    incentive_first_year = 0.0
    if dr_study is not None and "incentive_earned" in dr_study.performance:
        first = dr_study.performance[dr_study.performance["replicate"] == 0]
        first[["event_id", "start", "end", "settlement_basis", "incentive_earned"]].to_csv(
            package / "dr_incentives.csv", index=False)
        incentive_first_year = float(first["incentive_earned"].sum())
    if incentives is not None:  # what the measure adds: an events-only comparison home earns some too
        incentive_first_year = (incentives[1] or 0.0) - (incentives[0] or 0.0)

    bill_reduction = None
    if bills and {"without", "with"} <= set(bills):
        bill_reduction = float(bills["without"].summary["net_cost"] - bills["with"].summary["net_cost"])
    measure = {
        "spec": "docs/COST_TEST_SPEC.md v1.0",
        "created": datetime.now().isoformat(timespec="seconds"),
        "measure_id": config.name, "without": without_label, "without_modes": without_modes,
        "analysis_year": int(pd.DatetimeIndex(time_grid.timestamps())[0].year),
        "start_year": ct.start_year, "measure_life_years": ct.measure_life_years,
        "net_to_gross": ct.net_to_gross, "impact_degradation": ct.impact_degradation,
        "bill_escalation": ct.bill_escalation, "cost_escalation": ct.cost_escalation,
        "participant_costs": ct.participant_costs.model_dump(), "program_costs": ct.program_costs.model_dump(),
        "tax_credits": ct.tax_credits, "avoided_cost_set": ct.avoided_costs.set,
        "discount_rates": ct.discount_rates.model_dump() if ct.discount_rates else None, "tests": list(ct.tests),
        "capacity_value": chosen, "first_year": {
            "energy_impact_kwh": round(float(energy["load_reduction_kwh"].sum()), 3),
            "decrease_kwh": round(float(energy["decrease_kwh"].sum()), 3),
            "increase_kwh": round(float(energy["increase_kwh"].sum()), 3),
            "bill_reduction": bill_reduction, "dr_incentive": incentive_first_year,
        },
        "clock": f"energy_impact.csv is in standard time ({energy['timestamp'].iloc[0][-6:]}), every hour once",
    }
    (package / "measure.yaml").write_text(yaml.safe_dump(measure, sort_keys=False), encoding="utf-8")
    return package
