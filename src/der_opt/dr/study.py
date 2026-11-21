"""A demand response study for one measure (docs/DR_CAPACITY_SPEC.md v1.0).

Runs, all on the same year:
  R1  counterfactual: the scenario's rule-based policies, no event response
  R2  non-responding twin: the same optimization, never told about events
  R3  no measure: the measure's devices removed, optimized the same way
  responding: the optimization with the program's response to the calendar
      (one per calendar: a randomized constructor with replicates makes several)
and then per-event performance against R1, R2, R3 and the settlement baseline S --
every reduction on two bases, net load (exports count) and grid import (they don't) --
accounted capacity (called events, test events included), counted capacity (shadow
events on every eligible day), the Slice-of-Day profile, and DR checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from der_opt.diagnostics import CheckReport, Finding
from der_opt.dr import calendar as cal
from der_opt.dr.response import DREventResponse
from der_opt.dr.settlement import baseline_series, settlement_baselines
from der_opt.dr.shadow import shadow_event
from der_opt.results.schema import DispatchResult
from der_opt.scenarios.loader import (
    _build_price_series,
    _build_time_grid,
    build_counterfactual_policies,
    build_premise,
)
from der_opt.scenarios.schema import ScenarioConfig

REFERENCES = ("R1", "R2", "R3", "S")
GAP_TOLERANCE = 0.25


@dataclass
class DRStudy:
    program: object
    calendars: list[list[cal.Event]]
    references: dict[str, DispatchResult]  # R1, R2, R3
    responding: list[DispatchResult]  # one per calendar
    events: pd.DataFrame  # the calendar(s), with replicate
    performance: pd.DataFrame  # per event per replicate
    capacity: pd.DataFrame  # per month: counted and accounted, per reference
    slice_of_day: pd.DataFrame  # month x hour, per reference
    checks: CheckReport
    summary: dict = field(default_factory=dict)


def _net(result: DispatchResult) -> pd.Series:
    return result.grid_import - result.grid_export


def _calendars(config: ScenarioConfig, time_grid) -> tuple[list[list[cal.Event]], np.ndarray | None]:
    dr = config.dr
    events, program = dr.events, dr.program
    given = [name for name in ("file", "price", "random", "schedule") if getattr(events, name) is not None]
    if len(given) != 1:
        raise ValueError(f"dr.events needs exactly one constructor (file, price, random or schedule); got {given or 'none'}")
    if events.file is not None:
        return [cal.from_file(events.file, program, time_grid, dr.tz)], None
    if events.price is not None:
        price = _build_price_series(events.price.series).values(time_grid)
        return [cal.price_based(price, program, time_grid, events.price)], price
    if events.random is not None:
        price = _build_price_series(events.random.series).values(time_grid) if events.random.series else None
        return [cal.randomized(program, time_grid, events.random, r, price) for r in range(events.random.replicates)], price
    return [cal.schedule(events.schedule, program, time_grid)], None


def _without_measure(config: ScenarioConfig) -> ScenarioConfig:
    names = {d.name for d in config.devices}
    measure = set(config.dr.measure_devices) if config.dr.measure_devices is not None else names
    unknown = measure - names
    if unknown:
        raise ValueError(f"dr.measure_devices {sorted(unknown)} aren't devices in this scenario")
    kept = [d for d in config.devices if d.name not in measure]
    for device in kept:
        sources = set(device.params.get("charge_sources", [])) & measure
        if sources:
            raise ValueError(f"{device.name} charges from {sorted(sources)}, which R3 removes with the measure")
    return config.model_copy(update={"devices": kept, "counterfactual": None})


def _block(value: pd.Series, day, program, time_grid, steps: int) -> pd.DatetimeIndex:
    window = cal._steps_in_window(day, program, time_grid)
    sums = value.loc[window].rolling(steps).sum().shift(-(steps - 1)).dropna()
    start = sums.idxmax()
    return pd.date_range(start, periods=steps, freq=f"{time_grid.resolution_minutes}min")


def _flex_kw(premise) -> float:
    """The most a measure's devices could move net load in one step."""
    total = 0.0
    for d in premise.devices:
        # storage can swing from full charging to full discharging
        total += getattr(d, "p_max_charge_kw", 0) + getattr(d, "p_max_discharge_kw", 0)
        total += getattr(d, "p_max_kw", 0)  # thermostat
        total += getattr(d, "block_size_kw", 0) * getattr(d, "max_blocks", 0)  # curtailable load
    return total


def run_dr_study(config: ScenarioConfig, r2: DispatchResult | None = None, r2_premise=None) -> DRStudy:
    dr, program = config.dr, config.dr.program
    time_grid = _build_time_grid(config.time_grid)
    index = pd.DatetimeIndex(time_grid.timestamps())
    dt = time_grid.dt_hours
    checks = CheckReport()

    calendars, price = _calendars(config, time_grid)
    checks.ran("dr calendar")

    if r2 is None or r2_premise is None:
        r2_premise = build_premise(config)
        r2 = r2_premise.solve()[0]
    r1_premise = build_premise(config)
    policies = build_counterfactual_policies(config, r1_premise) or {}
    from der_opt.simulation import CounterfactualSimulator

    r1 = CounterfactualSimulator(r1_premise, policies).run()
    r3 = build_premise(_without_measure(config)).solve()[0]
    refs = {"R1": r1, "R2": r2, "R3": r3}
    nets = {name: _net(result) for name, result in refs.items()}
    # two bases for every reduction (spec v1.1): net load, so exports count, and grid
    # import only (each step's load floored at zero), so they don't
    loads = {"net": nets, "import": {name: series.clip(lower=0) for name, series in nets.items()}}
    prefix = {"net": "", "import": "import_"}

    responding, perf_rows, event_rows = [], [], []
    for replicate, events in enumerate(calendars):
        mask = np.zeros(len(index), dtype=bool)
        for e in events:
            mask |= (index >= e.start) & (index < e.end)
        basis = program.settlement_basis
        # the expected baseline the optimization settles against: R2 on the program's basis
        expected = baseline_series(settlement_baselines(loads[basis]["R2"], events, dr.customer_class), events, index)
        premise = build_premise(config)
        premise.value_streams.append(DREventResponse(program, mask, expected.to_numpy(), time_grid))
        result = premise.solve()[0]
        responding.append(result)
        net = _net(result)
        mine = {"net": net, "import": net.clip(lower=0)}
        settled = {basis: settlement_baselines(mine[basis], events, dr.customer_class) for basis in mine}
        s_series = {basis: baseline_series(settled[basis], events, index) for basis in mine}
        for e in events:
            rows = (index >= e.start) & (index < e.end)
            row = {"replicate": replicate, "event_id": e.event_id, "event_type": e.event_type, "month": e.start.month,
                   "start": e.start, "end": e.end, "hours": e.hours, "response": program.response}
            for each_basis, pre in prefix.items():  # not `basis`: that's the program's settlement basis
                ours = float(mine[each_basis][rows].mean())
                row[f"responding_{pre}kw"] = ours
                for name in ("R1", "R2", "R3"):
                    theirs = float(loads[each_basis][name][rows].mean())
                    row[f"{name}_{pre}kw"] = theirs
                    row[f"{pre}reduction_vs_{name}_kw"] = theirs - ours
                row[f"S_{pre}kw"] = float(s_series[each_basis][rows].mean())
                row[f"{pre}reduction_vs_S_kw"] = row[f"S_{pre}kw"] - ours
            row["settlement_basis"] = basis
            row["S_expected_kw"] = float(expected[rows].mean())
            row["S_adjustment"] = settled[basis][e.event_id].adjustment
            row["S_days"] = len(settled[basis][e.event_id].days_used)
            s_settle, settle_load = s_series[basis], mine[basis]
            if program.response == "incentive":
                kwh = float(((s_settle[rows] - settle_load[rows]) * dt).sum())
                row["incentive_earned"] = program.incentive_per_kwh * max(0.0, kwh)
            elif program.response == "commitment":
                target = s_settle[rows] - program.committed_kw
                row["shortfall_kwh"] = float((np.clip(settle_load[rows] - target, 0, None) * dt).sum())
            else:
                row["shortfall_kwh"] = float((np.clip(result.grid_import[rows] - program.cap_kw, 0, None) * dt).sum())
            after = (index >= e.end) & (index.date == e.day)
            row["rebound_kwh"] = float(((net[after] - nets["R2"][after]) * dt).sum())
            row["settlement_note"] = settled[basis][e.event_id].note
            perf_rows.append(row)
        event_rows.append(cal.calendar_frame(events).assign(replicate=replicate))
    performance = pd.DataFrame(perf_rows)
    events_frame = pd.concat(event_rows, ignore_index=True) if event_rows else pd.DataFrame()

    # --- counted capacity: shadow events on every eligible day in season
    chosen = ["R2", "R3", "S"] if program.capacity_reference == "all" else [program.capacity_reference]
    counted_rows = []
    if dr.shadow_events:
        if dr.value_series is not None:
            value = pd.Series(_build_price_series(dr.value_series).values(time_grid), index=index)
        elif price is not None:
            value = pd.Series(price, index=index)
        else:
            value = nets["R2"]  # no value series: place the block on R2's highest load
        steps = round(program.max_hours / dt)
        for day in cal.eligible_days(program, time_grid):
            block = _block(value, day, program, time_grid, steps)
            day_start = int(index.get_loc(pd.Timestamp(day)))
            block_start = int(index.get_loc(block[0]))
            shadow = shadow_event(r2_premise, r2, day_start, block_start, steps)
            shadow_load = {"net": shadow, "import": np.clip(shadow, 0, None)}
            hypothetical = cal.Event(f"shadow-{day}", program.program_id, "shadow", block[0],
                                     block[-1] + pd.Timedelta(minutes=time_grid.resolution_minutes))
            row = {"day": day, "month": day.month, "start": block[0]}
            for basis, pre in prefix.items():
                ours = float(shadow_load[basis].mean())
                for name in ("R2", "R3"):
                    row[f"{pre}{name}"] = float(loads[basis][name].loc[block].mean()) - ours
                s_hyp = settlement_baselines(loads[basis]["R2"], [hypothetical], dr.customer_class)[hypothetical.event_id].values
                row[f"{pre}S"] = float(np.nanmean(s_hyp)) - ours if np.isfinite(s_hyp).any() else np.nan
            counted_rows.append(row)
    counted = pd.DataFrame(counted_rows)

    # --- capacity table: per month, counted and accounted, per chosen reference, both bases
    cap_rows = []
    for month in range(1, 13):
        row = {"month": month, "in_season": month in program.season}
        month_counted = counted[counted["month"] == month] if len(counted) else counted
        row["shadow_days"] = len(month_counted)
        month_perf = performance[performance["month"] == month] if len(performance) else performance
        row["events"] = len(month_perf) / max(len(calendars), 1)
        row["test_events"] = int((month_perf["event_type"] == "test").sum() / max(len(calendars), 1)) if len(month_perf) else 0
        row["event_hours"] = float(month_perf["hours"].sum() / max(len(calendars), 1)) if len(month_perf) else 0.0
        for pre in prefix.values():
            for name in chosen:
                if len(month_counted):
                    values = month_counted[f"{pre}{name}"]
                    row[f"counted_{pre}{name}_kw"] = values.mean()
                    row[f"counted_{pre}{name}_min_kw"] = values.min()
                    row[f"counted_{pre}{name}_max_kw"] = values.max()
                if len(month_perf):
                    # hour-weighted, per replicate, then across replicates
                    per_rep = month_perf.groupby("replicate").apply(
                        lambda g, c=f"{pre}reduction_vs_{name}_kw": np.average(g[c], weights=g["hours"]),
                        include_groups=False)
                    row[f"accounted_{pre}{name}_kw"] = float(per_rep.mean())
                    if len(calendars) > 1:
                        row[f"accounted_{pre}{name}_min_kw"] = float(per_rep.min())
                        row[f"accounted_{pre}{name}_max_kw"] = float(per_rep.max())
        cap_rows.append(row)
    capacity = pd.DataFrame(cap_rows)

    # --- Slice-of-Day: month x hour, accounted from event days (all 24 hours, so a
    # rebound shows), counted from shadow events in months without events
    sod_rows = []
    hour_of = index.hour
    for month in range(1, 13):
        month_events = [(r, e) for r, evs in enumerate(calendars) for e in evs if e.start.month == month]
        for basis, pre in prefix.items():
            if month_events:
                for name in chosen:
                    profiles = []
                    for r, e in month_events:
                        day_rows = index.date == e.day
                        ours = _net(responding[r])
                        ours = ours.clip(lower=0) if basis == "import" else ours
                        if name == "S":
                            theirs = baseline_series(settlement_baselines(ours, calendars[r], dr.customer_class),
                                                     calendars[r], index)
                        else:
                            theirs = loads[basis][name]
                        reduction = theirs[day_rows] - ours[day_rows]
                        profiles.append(reduction.groupby(hour_of[day_rows]).mean())
                    mean = pd.concat(profiles, axis=1).mean(axis=1)
                    sod_rows += [{"month": month, "hour": h, "reference": name, "basis": basis, "source": "accounted",
                                  "reduction_kw": mean.get(h, np.nan)} for h in range(24)]
            elif len(counted) and (counted["month"] == month).any():
                month_counted = counted[counted["month"] == month]
                for name in chosen:
                    by_hour = {}
                    for _, c in month_counted.iterrows():
                        hours = pd.date_range(c["start"], periods=round(program.max_hours / dt),
                                              freq=f"{time_grid.resolution_minutes}min").hour
                        for h in set(hours):
                            by_hour.setdefault(h, []).append(c[f"{pre}{name}"])
                    sod_rows += [{"month": month, "hour": h, "reference": name, "basis": basis, "source": "counted",
                                  "reduction_kw": float(np.mean(by_hour[h])) if h in by_hour else np.nan}
                                 for h in range(24)]
    slice_of_day = pd.DataFrame(sod_rows)

    # --- checks
    flex = _flex_kw(r2_premise)
    found = []
    if len(performance):
        over = performance[performance["reduction_vs_R2_kw"] > flex + 1e-3]
        if len(over):
            found.append(Finding("warning", "dr reduction vs devices", f"{len(over)} events reduce more than the devices' "
                                 f"combined {flex:g} kW could (largest {over['reduction_vs_R2_kw'].max():.2f} kW)"))
    checks.ran("dr reduction vs devices", found)
    found = []
    if len(performance):
        rebound = performance[performance["rebound_kwh"] > 0.5]
        if len(rebound):
            found.append(Finding("warning", "dr rebound", f"{len(rebound)} of {len(performance)} events are followed by "
                                 f"extra load the same day vs R2 (up to {rebound['rebound_kwh'].max():.1f} kWh)"))
    checks.ran("dr rebound", found)
    found = []
    if len(performance):
        base = performance["reduction_vs_R2_kw"].abs()
        gap = (performance["reduction_vs_S_kw"] - performance["reduction_vs_R2_kw"]).abs()
        off = performance[(base > 1e-6) & (gap > GAP_TOLERANCE * base)]
        if len(off):
            found.append(Finding("warning", "dr settlement vs load impact", f"{len(off)} of {len(performance)} events "
                                 "settle more than 25% away from their R2 load impact"))
        missing = performance[performance["settlement_note"] == "no eligible baseline days"]
        if len(missing):
            found.append(Finding("warning", "dr settlement vs load impact", f"{len(missing)} events have no eligible "
                                 "baseline days (too early in the run); their settlement is empty"))
        short = performance.get("shortfall_kwh")
        if short is not None and (short > 1e-6).any():
            found.append(Finding("warning", "dr shortfall", f"{int((short > 1e-6).sum())} events fall short of the "
                                 f"{program.response} obligation ({short.sum():.1f} kWh in all)"))
    checks.ran("dr settlement vs load impact", found)
    found = []
    if "counted_R2_kw" in capacity and "accounted_R2_kw" in capacity:
        both = capacity.dropna(subset=["counted_R2_kw", "accounted_R2_kw"])
        off = both[(both["counted_R2_kw"].abs() > 1e-6)
                   & ((both["accounted_R2_kw"] - both["counted_R2_kw"]).abs() > GAP_TOLERANCE * both["counted_R2_kw"].abs())]
        if len(off):
            mean_hours = performance["hours"].mean()
            note = (f"; called events average {mean_hours:g} h and shadow events last {program.max_hours:g} h, so "
                    "some of the gap is event length" if abs(mean_hours - program.max_hours) > 1e-9 else "")
            found.append(Finding("warning", "dr accounted vs counted", "accounted capacity is more than 25% away from "
                                 f"counted capacity in month(s) {', '.join(str(m) for m in off['month'])}{note}"))
    checks.ran("dr accounted vs counted", found)

    summary = {
        "program": program.program_id, "response": program.response, "settlement_basis": program.settlement_basis,
        "customer_class": dr.customer_class,
        "capacity_reference": program.capacity_reference, "calendars": len(calendars),
        "constructor": next(n for n in ("file", "price", "random", "schedule") if getattr(dr.events, n) is not None),
        "events_per_calendar": [len(c) for c in calendars], "weather_case": config.weather_case,
        "incentive_earned": float(performance["incentive_earned"].sum() / len(calendars))
        if "incentive_earned" in performance else None,
    }
    return DRStudy(program, calendars, refs, responding, events_frame, performance, capacity, slice_of_day, checks, summary)
