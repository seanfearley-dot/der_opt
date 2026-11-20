"""Data-quality checks on a prepared kW series: the warnings, not the rules. The spec
reader (der_opt.inputs.spec) runs these on every power column."""

from __future__ import annotations

import pandas as pd

from der_opt.diagnostics import CheckReport, Finding


def _finding(level: str, check: str, message: str) -> Finding:
    return Finding(level, check, message)


def _runs(mask: pd.Series) -> list[tuple[pd.Timestamp, int]]:
    """(start, length) of each run of True."""
    values = mask.to_numpy()
    runs, start = [], None
    for i, flag in enumerate(values):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((mask.index[start], i - start))
            start = None
    if start is not None:
        runs.append((mask.index[start], len(values) - start))
    return runs


# --- quality checks on a prepared series --------------------------------------------


def check_series(
    series: pd.Series,
    resolution_minutes: int | None = None,
    label: str = "series",
    import_kw: pd.Series | None = None,
    zero_run_hours: float = 24,
    flat_run_hours: float = 12,
    home_peak_kw: float | None = None,
    check_zero_runs: bool = True,
) -> CheckReport:
    """Checks a timestamp-indexed kW series as a scenario would read it: regular steps,
    no duplicates, missing values, long runs of zero import (outage or a failed
    meter), long flat runs (estimated reads), spikes, and values that look like the
    wrong unit."""
    report = CheckReport()
    s = series.sort_index()
    index = pd.DatetimeIndex(s.index)
    if index.tz is not None:
        report.ran("clock", [_finding("error", "clock", f"{label}: timestamps carry a time zone; a scenario needs "
                                      "the local clock without one")])
    duplicates = int(index.duplicated().sum())
    report.ran("duplicates", [_finding("error", "duplicates", f"{label}: {duplicates} repeated timestamps")]
               if duplicates else [])
    steps = pd.Series(index).diff().dropna()
    if resolution_minutes is None and len(steps):
        resolution_minutes = round(steps.median() / pd.Timedelta(minutes=1))
    step = pd.Timedelta(minutes=resolution_minutes or 60)
    irregular = int((steps != step).sum())
    report.ran("regular steps", [_finding("warning", "regular steps", f"{label}: {irregular} steps aren't "
                                          f"{step}; a scenario fills between them")] if irregular else [])
    missing = int(s.isna().sum())
    report.ran("missing values", [_finding("warning", "missing values", f"{label}: {missing} of {len(s)} steps "
                                           "empty; a scenario repeats the value before each")] if missing else [])

    per_hour = pd.Timedelta(hours=1) / step
    found = []
    imports = (import_kw if import_kw is not None else s.clip(lower=0)).sort_index()
    for start, length in _runs(imports.fillna(-1).eq(0)) if check_zero_runs else []:
        if length / per_hour >= zero_run_hours:
            found.append(_finding("warning", "zero runs", f"{label}: no import for {length / per_hour:g} h from "
                                  f"{start} -- an outage, a vacancy, or a failed meter?"))
    report.ran("zero runs", found[:5])
    found = []
    values = s.dropna()
    changes = values.ne(values.shift())
    run_id = changes.cumsum()
    for _, run in values.groupby(run_id):
        if len(run) / per_hour >= flat_run_hours and run.iloc[0] != 0:
            found.append(_finding("warning", "flat runs", f"{label}: {run.iloc[0]:g} kW unchanged for "
                                  f"{len(run) / per_hour:g} h from {run.index[0]} -- estimated reads?"))
    report.ran("flat runs", found[:5])

    found = []
    if len(values):
        peak = float(values.abs().max())
        p99 = float(values.abs().quantile(0.99))
        if peak > 1000:
            found.append(_finding("warning", "units", f"{label}: peaks at {peak:,.0f} kW -- W read as kW?"))
        elif home_peak_kw is not None and peak > home_peak_kw:
            found.append(_finding("warning", "units", f"{label}: peaks at {peak:,.1f} kW -- fine for a large "
                                  "site, but for a home check the unit (W read as kW?)"))
        elif 0 < peak < 0.05:
            found.append(_finding("warning", "units", f"{label}: never above {peak:.4f} kW -- kWh read as Wh, "
                                  "or MW as kW?"))
        if p99 > 0 and peak > 10 * p99 and peak > 5:
            found.append(_finding("warning", "spikes", f"{label}: peak {peak:,.1f} kW is over 10x the 99th "
                                  f"percentile ({p99:,.1f} kW) at {values.abs().idxmax()}"))
    report.ran("units and spikes", found)
    return report
