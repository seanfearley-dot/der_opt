"""Checks run after every solve: does the result obey its own physics, and is anything
about it likely to mislead?

  error    the result is internally inconsistent -- a bug, not a modeling choice
           (energy doesn't balance, storage outside its limits, the objective
           disagrees with re-pricing the same dispatch)
  warning  the result is consistent but could mislead (importing and exporting in the
           same hour, runs ending with different stored energy, a tariff out of date,
           input data with gaps, prices that look like $/MWh)
  info     an expected conversion worth knowing about (daylight-saving days moved
           onto the local clock); doesn't count against a check

Every check works on a DispatchResult, optimized or simulated, so the same checks
apply to the optimal run, the counterfactual, and every premise on a feeder.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from der_opt.results.schema import DispatchResult

TOLERANCE_KW = 1e-3


@dataclass(frozen=True)
class Finding:
    level: str  # "error", "warning" or "info"
    check: str
    message: str
    run: str | None = None
    premise: str | None = None


@dataclass
class CheckReport:
    """Which checks ran and what they found."""

    checks_run: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def ran(self, check: str, findings: list[Finding] = ()) -> None:
        if check not in self.checks_run:
            self.checks_run.append(check)
        self.findings.extend(findings)

    def extend(self, other: CheckReport) -> None:
        for check in other.checks_run:
            self.ran(check)
        self.findings.extend(other.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    def summary(self) -> str:
        failed = {f.check for f in self.findings if f.level != "info"}
        passed = [c for c in self.checks_run if c not in failed]
        return f"{len(passed)} of {len(self.checks_run)} checks clean, {len(self.warnings)} warnings, {len(self.errors)} errors"

    def lines(self, max_premises: int = 3) -> list[str]:
        """Findings for printing. On a feeder, the same finding on many premises is
        collapsed into one line naming the first few."""
        grouped: dict[tuple, list[Finding]] = {}
        for finding in self.findings:
            grouped.setdefault((finding.level, finding.check, finding.run, finding.premise is None), []).append(finding)
        out = []
        order = {"error": 0, "warning": 1, "info": 2}
        for (level, check, run, _), group in sorted(grouped.items(), key=lambda kv: (order.get(kv[0][0], 3), kv[0][1])):
            where = f" ({run})" if run else ""
            if len(group) == 1 or group[0].premise is None:
                for finding in group:
                    who = f"{finding.premise}: " if finding.premise else ""
                    out.append(f"{level.upper()} {check}{where}: {who}{finding.message}")
            else:
                shown = "; ".join(f"{f.premise}: {f.message}" for f in group[:max_premises])
                more = f"; and {len(group) - max_premises} more" if len(group) > max_premises else ""
                out.append(f"{level.upper()} {check}{where}, {len(group)} premises: {shown}{more}")
        return out

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(f) for f in self.findings], columns=["level", "check", "message", "run", "premise"])


def _hours_and_kwh(mask: np.ndarray, amount: np.ndarray, dt: float) -> str:
    return f"{mask.sum() * dt:g} h, {float(amount[mask].sum() * dt):.2f} kWh"


def check_dispatch(premise, dispatch: DispatchResult, run: str | None = None, name: str | None = None) -> CheckReport:
    """Physics and consistency of one premise's dispatch. `name` labels findings by
    premise (for feeders); a single-premise run leaves it None."""
    report = CheckReport()
    dt = premise.time_grid.dt_hours
    imp = dispatch.grid_import.to_numpy(dtype=float)
    exp = dispatch.grid_export.to_numpy(dtype=float)
    columns = dispatch.device_dispatch.columns

    def finding(level, check, message):
        return Finding(level, check, message, run, name)

    # energy balance at the meter
    device_power = sum(
        (dispatch.device_dispatch[f"{d.name}.net_power"].to_numpy(dtype=float) for d in premise.devices
         if f"{d.name}.net_power" in columns),
        np.zeros(len(imp)),
    )
    residual = imp - exp + device_power - premise.external_load
    worst = float(np.abs(residual).max()) if len(residual) else 0.0
    scale = max(1.0, float(np.abs(premise.external_load).max(initial=0)))
    report.ran("energy balance", [finding(
        "error", "energy balance",
        f"grid + devices miss the load by up to {worst:.4f} kW (at step {int(np.abs(residual).argmax())})",
    )] if worst > 1e-4 * scale + TOLERANCE_KW else [])

    # meter limit
    found = []
    for direction, flow, limit in (("import", imp, premise.import_limit_kw), ("export", exp, premise.export_limit_kw)):
        if limit is not None and flow.max(initial=0) - limit > TOLERANCE_KW:
            found.append(finding("error", "meter limit", f"{direction} reaches {flow.max():.3f} kW, over its "
                                 f"{limit:g} kW limit"))
    report.ran("meter limit", found)

    # storage within limits, and not charging and discharging at once
    bounds, both_ways = [], []
    for device in premise.devices:
        soc_col = f"{device.name}.soc"
        if soc_col in columns and hasattr(device, "soc_min"):
            soc = dispatch.device_dispatch[soc_col].to_numpy(dtype=float)
            end = dispatch.final_state.get(device.name, {}).get("soc")
            soc = np.append(soc, end) if end is not None else soc
            low, high = device.soc_min - TOLERANCE_KW, device.soc_max + TOLERANCE_KW
            if soc.min() < low or soc.max() > high:
                bounds.append(finding("error", "storage limits",
                                      f"{device.name} charge goes to {soc.min():.2f}-{soc.max():.2f} kWh, "
                                      f"outside {device.soc_min:g}-{device.soc_max:g}"))
        ch, dis = f"{device.name}.p_ch", f"{device.name}.p_dis"
        if ch in columns and dis in columns:
            p_ch = dispatch.device_dispatch[ch].to_numpy(dtype=float)
            p_dis = dispatch.device_dispatch[dis].to_numpy(dtype=float)
            mask = (p_ch > TOLERANCE_KW) & (p_dis > TOLERANCE_KW)
            if mask.any():
                both_ways.append(finding("warning", "charge and discharge at once",
                                         f"{device.name}: {_hours_and_kwh(mask, np.minimum(p_ch, p_dis), dt)} -- pure loss"))
    report.ran("storage limits", bounds)
    report.ran("charge and discharge at once", both_ways)

    # importing and exporting in the same step
    mask = (imp > TOLERANCE_KW) & (exp > TOLERANCE_KW)
    report.ran("import and export at once", [finding(
        "warning", "import and export at once",
        f"{_hours_and_kwh(mask, np.minimum(imp, exp), dt)}; a bill credits that export even though no "
        "energy flowed out (set exclusive_import_export: true to forbid it)",
    )] if mask.any() else [])

    # the objective should equal the same dispatch re-priced by each stream's own
    # numeric evaluation -- catches unit slips such as a missing x dt
    if dispatch.solver_status != "simulated":
        from der_opt.pricing import value_breakdown

        try:
            repriced = sum(value_breakdown(premise, dispatch).values())
        except NotImplementedError:
            repriced = None
        if repriced is not None:
            gap = abs(dispatch.objective_value - repriced)
            report.ran("objective matches re-pricing", [finding(
                "error", "objective matches re-pricing",
                f"solver objective ${dispatch.objective_value:,.2f} vs ${repriced:,.2f} re-priced from the dispatch",
            )] if gap > max(0.01, 1e-4 * abs(repriced)) else [])
    return report


def check_end_states(premise, runs: dict[str, DispatchResult], name: str | None = None, tolerance_frac: float = 0.01) -> CheckReport:
    """Stored energy at the end isn't valued, so a run that ends with less looks better
    by roughly that energy's worth. Flags runs ending differently from each other, or
    (for a lone run) below where storage started."""
    report = CheckReport()
    found = []
    for device in premise.devices:
        ends = {label: r.final_state.get(device.name, {}).get("soc") for label, r in runs.items()}
        ends = {label: end for label, end in ends.items() if end is not None}
        if not ends:
            continue
        tolerance = tolerance_frac * getattr(device, "capacity_kwh", 1.0)
        start = getattr(device, "soc_init", None)
        described = ", ".join(f"{end:.1f} ({label})" for label, end in ends.items())
        if len(ends) > 1 and max(ends.values()) - min(ends.values()) > tolerance:
            low = min(ends.values())
            favored = [label for label, end in ends.items() if end - low <= tolerance]
            runs_text = " and ".join(favored) + (" runs" if len(favored) > 1 else " run")
            found.append(Finding("warning", "end state", f"{device.name} ends at {described} kWh; unvalued stored "
                                 f"energy favors the {runs_text}", None, name))
        elif len(ends) == 1 and start is not None and start - next(iter(ends.values())) > tolerance:
            found.append(Finding("warning", "end state", f"{device.name} ends at {described} kWh, below its "
                                 f"{start:.1f} kWh start; the result counts that energy as free", None, name))
    report.ran("end state", found)
    return report


def check_prices(premise, name: str | None = None) -> CheckReport:
    """$/kWh prices outside a believable range usually mean a unit slip ($/MWh, cents)."""
    from der_opt.premise import EnergyArbitrage

    report = CheckReport()
    found = []
    dt = premise.time_grid.dt_hours
    for stream in premise.value_streams:
        if not isinstance(stream, EnergyArbitrage):
            continue
        for side, rates in (("import", stream._import_rates), ("export", stream._export_rates)):
            per_kwh = np.asarray(rates) / dt
            peak = float(np.abs(per_kwh).max(initial=0))
            if peak > 5:
                found.append(Finding("warning", "price units", f"{side} price reaches ${peak:,.2f}/kWh -- $/MWh not "
                                     "divided by 1000?", None, name))
            elif 0 < peak < 0.002:
                found.append(Finding("warning", "price units", f"{side} price never exceeds ${peak:.5f}/kWh -- "
                                     "divided by 1000 twice?", None, name))
    report.ran("price units", found)
    return report


def check_load(premise, name: str | None = None) -> CheckReport:
    found = []
    peak = float(np.max(premise.external_load, initial=0))
    if premise.import_limit_kw is not None and peak > premise.import_limit_kw:
        found.append(Finding("warning", "load vs meter limit", f"load peaks at {peak:.1f} kW, above the "
                             f"{premise.import_limit_kw:g} kW import limit; only on-site devices can make up the "
                             "difference", None, name))
    report = CheckReport()
    report.ran("load vs meter limit", found)
    return report


def check_tariff_dates(tariff, time_grid, max_age_days: int = 365) -> CheckReport:
    """Rates change several times a year; a run outside a tariff's dates uses the wrong ones."""
    report = CheckReport()
    found = []
    effective = getattr(tariff, "effective_date", "")
    if effective and time_grid.start is not None:
        effective_day = datetime.fromisoformat(str(effective))
        run_start = time_grid.start
        run_end = run_start + timedelta(hours=time_grid.horizon_steps * time_grid.dt_hours)
        if run_start < effective_day:
            found.append(Finding("warning", "tariff dates", f"{tariff.name} took effect {effective}, after this run "
                                 f"starts ({run_start.date()}); earlier rates applied then"))
        elif run_end > effective_day + timedelta(days=max_age_days):
            found.append(Finding("warning", "tariff dates", f"{tariff.name} rates are from {effective}, over "
                                 f"{max_age_days // 30} months before this run ends ({run_end.date()}); check for newer rates"))
    report.ran("tariff dates", found)
    return report


@contextmanager
def collect_data_warnings(report: CheckReport) -> Iterator[None]:
    """Turns DataGapWarnings raised while reading inputs into findings."""
    from der_opt.pricing import DataGapWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DataGapWarning)
        yield
    found = []
    for item in caught:
        if issubclass(item.category, DataGapWarning):
            found.append(Finding("warning", "input data", str(item.message)))
        else:
            warnings.showwarning(item.message, item.category, item.filename, item.lineno)
    report.ran("input data", list(dict.fromkeys(found)))
