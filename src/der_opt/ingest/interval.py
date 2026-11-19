"""Meter interval data -> a CSV a scenario can read, with every repair reported.

A scenario wants one row per step on the local clock, in kW:

    timestamp,import_kw,export_kw,net_kw
    2026-07-01 00:00:00,0.82,0.0,0.82

Utility extracts differ in every way that matters, and this module normalizes them
(the conventions follow loadcast's ingestion, so one config style works for both):

  - layout: long (one row per interval), or wide (a date and HE1..HE24/25 columns);
    a preamble above the header row is skipped
  - timestamps: one column, or a date and a time column; labels on a local clock or
    in UTC; interval beginning or ending
  - units: W/kW/MW (average power) or Wh/kWh/MWh (energy per interval)
  - channels: delivered and received (import, export), or one signed net channel
  - daylight saving: November's repeated hour -- the first label is daylight time;
    March's skipped hour shifts forward

Then it lines the data up on a regular grid, fills only short gaps, averages to the
requested resolution, and moves to the local clock, where every day has 24 hours:
November's two 1 am hours are averaged into one, and March's missing 2 am is
interpolated. Each of those steps that changes data adds a finding, so nothing is
repaired silently. check_series runs the same quality checks on any prepared series.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from der_opt.diagnostics import CheckReport, Finding

POWER_UNITS = {"w": 1e-3, "kw": 1.0, "mw": 1e3}
ENERGY_UNITS = {"wh": 1e-3, "kwh": 1.0, "mwh": 1e3}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimestampSpec(_Model):
    column: str | None = None  # one timestamp column...
    date_column: str | None = None  # ...or a date column (wide layout, or with time_column)
    time_column: str | None = None
    format: str | None = None  # strptime format of the (combined) label; None = infer
    tz: str = "America/Los_Angeles"  # zone of the labels; ignored when they carry an offset
    convention: Literal["beginning", "ending"] = "beginning"


class WideHours(_Model):
    pattern: str = "HE{h}"
    first: int = 1
    max_hours: int = 25


class IntervalSpec(_Model):
    """How to read one meter's interval file. See examples/ingest/ for a config."""

    path: str
    header_row: int | None = None  # 0-based line of the header; None = find it
    csv_options: dict[str, Any] = Field(default_factory=dict)
    timestamp: TimestampSpec = Field(default_factory=TimestampSpec)
    wide_hours: WideHours | None = None  # wide layout: hour columns hold `net` values
    columns: dict[str, str] = Field(default_factory=dict)  # delivered / received / net / meter_id -> source
    meter: str | None = None  # which meter, if the file has several
    unit: Literal["w", "kw", "mw", "wh", "kwh", "mwh"]
    interval_minutes: int | None = None  # None = infer
    resolution_minutes: int | None = None  # output step; None = the data's own interval
    min_coverage: float = 0.75  # share of a step's intervals needed to keep the step
    max_fill_intervals: int = 2  # interpolate gaps up to this many intervals; leave longer ones
    out_tz: str | None = None  # local clock for the output; default timestamp.tz


@dataclass
class IngestResult:
    table: pd.DataFrame  # timestamp index (local, naive), import_kw, export_kw, net_kw
    report: CheckReport
    summary: dict = field(default_factory=dict)


def load_interval_spec(path: str | Path) -> tuple[IntervalSpec, Path]:
    path = Path(path)
    return IntervalSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))), path.parent


# --- reading ------------------------------------------------------------------------


def _find_header(lines: list[str], wanted: list[str]) -> int:
    """First line that names every wanted column (skips account preambles)."""
    for number, line in enumerate(lines[:200]):
        cells = [c.strip().strip('"') for c in line.split(",")]
        if all(w in cells for w in wanted):
            return number
    raise ValueError(f"no header row naming {wanted} in the first 200 lines")


def _read(spec: IntervalSpec, base: Path) -> pd.DataFrame:
    path = Path(spec.path) if Path(spec.path).is_absolute() else base / spec.path
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    text = path.read_text(encoding="utf-8-sig")
    header = spec.header_row
    if header is None:
        ts = spec.timestamp
        wanted = [c for c in (ts.column, ts.date_column, ts.time_column) if c]
        header = _find_header(text.splitlines(), wanted)
    # nothing above the header is kept: account preambles hold names and addresses
    body = "\n".join(text.splitlines()[header:])
    return pd.read_csv(io.StringIO(body), **spec.csv_options)


# --- timestamps ---------------------------------------------------------------------


def localize_labels(naive: pd.Series, tz: str) -> pd.Series:
    """Local-clock labels -> UTC instants. A repeated November label is daylight time
    the first time and standard time the second; a skipped March label moves forward
    to the same instant as the next hour (as loadcast.data.ingest does)."""
    parsed = pd.to_datetime(naive)
    if parsed.dt.tz is not None:
        return parsed.dt.tz_convert("UTC")
    first = (parsed.groupby(parsed, sort=False).cumcount() == 0).to_numpy()
    local = pd.DatetimeIndex(parsed).tz_localize(tz, ambiguous=first, nonexistent="shift_forward")
    return pd.Series(local.tz_convert("UTC"), index=naive.index)


def _labels(df: pd.DataFrame, ts: TimestampSpec) -> pd.Series:
    if ts.column:
        raw = df[ts.column].astype(str)
    elif ts.date_column and ts.time_column:
        raw = df[ts.date_column].astype(str).str.strip() + " " + df[ts.time_column].astype(str).str.strip()
    else:
        raise ValueError("timestamp needs `column`, or `date_column` and `time_column`")
    return pd.to_datetime(raw, format=ts.format) if ts.format else pd.to_datetime(raw)


def _wide_to_long(df: pd.DataFrame, spec: IntervalSpec) -> pd.DataFrame:
    wh, ts = spec.wide_hours, spec.timestamp
    if not ts.date_column:
        raise ValueError("wide layout needs timestamp.date_column")
    hour_cols = {wh.pattern.format(h=h): h - wh.first for h in range(wh.first, wh.first + wh.max_hours)}
    present = [c for c in hour_cols if c in df.columns]
    if not present:
        raise KeyError(f"no hour columns matching {wh.pattern!r}")
    long = df.melt(id_vars=[ts.date_column], value_vars=present, var_name="_h", value_name="value").dropna(subset=["value"])
    days = pd.to_datetime(long[ts.date_column], format=ts.format).dt.normalize()
    # hour k of a day is k hours after local midnight in real time, so a 25-hour day's
    # HE25 lands after 11 pm standard time and a 23-hour day has no HE24
    unique = pd.DatetimeIndex(days.unique())
    midnight = pd.Series(unique.tz_localize(ts.tz).tz_convert("UTC"), index=unique)
    next_midnight = pd.Series((unique + pd.Timedelta(days=1)).tz_localize(ts.tz).tz_convert("UTC"), index=unique)
    offset = long["_h"].map(hour_cols).to_numpy()
    start = pd.DatetimeIndex(midnight.reindex(days).array)
    hours_in_day = ((next_midnight - midnight) / pd.Timedelta(hours=1)).reindex(days).to_numpy()
    keep = offset < hours_in_day
    return pd.DataFrame({"utc": (start + pd.to_timedelta(offset, unit="h"))[keep], "net": long["value"].to_numpy()[keep]})


# --- the pipeline -------------------------------------------------------------------


def _finding(level: str, check: str, message: str) -> Finding:
    return Finding(level, check, message)


def ingest_interval_data(spec: IntervalSpec, base_dir: str | Path = ".") -> IngestResult:
    """Reads the file the spec names and runs the pipeline."""
    return ingest_frame(_read(spec, Path(base_dir)), spec)


def ingest_frame(raw: pd.DataFrame, spec: IntervalSpec) -> IngestResult:
    """The pipeline on a table already read (spec.path is ignored)."""
    report = CheckReport()
    cols = spec.columns

    if "meter_id" in cols:
        ids = raw[cols["meter_id"]].astype(str)
        meters = ids.unique()
        if spec.meter is None and len(meters) > 1:
            raise ValueError(f"the file has {len(meters)} meters; choose one with `meter` (e.g. {meters[0]!r})")
        if spec.meter is not None:
            raw = raw[ids == str(spec.meter)]
            if raw.empty:
                raise ValueError(f"meter {spec.meter!r} isn't in the file")

    # --- channels to a long frame of (UTC instant, import, export) in source units
    if spec.wide_hours is not None:
        long = _wide_to_long(raw, spec)
        minutes = 60
    else:
        labels = _labels(raw, spec.timestamp)
        utc = localize_labels(labels, spec.timestamp.tz)
        frame = {"utc": utc.to_numpy()}
        if "delivered" in cols:
            frame["delivered"] = pd.to_numeric(raw[cols["delivered"]], errors="coerce").to_numpy()
            frame["received"] = (pd.to_numeric(raw[cols["received"]], errors="coerce").to_numpy()
                                 if "received" in cols else np.zeros(len(raw)))
        elif "net" in cols:
            frame["net"] = pd.to_numeric(raw[cols["net"]], errors="coerce").to_numpy()
        else:
            raise ValueError("columns needs `delivered` (and optionally `received`), or `net`")
        long = pd.DataFrame(frame).dropna(subset=["utc"])
        diffs = pd.Series(long["utc"].sort_values().unique()).diff().dropna()
        minutes = spec.interval_minutes or (round(diffs.median() / pd.Timedelta(minutes=1)) if len(diffs) else 60)
        if spec.timestamp.convention == "ending":
            long["utc"] = long["utc"] - pd.Timedelta(minutes=minutes)

    if "net" in long:
        net = long.pop("net")
        long["delivered"], long["received"] = net.clip(lower=0), (-net).clip(lower=0)
        if (net < 0).any():
            report.ran("channels", [_finding("warning", "channels", "one signed net channel: import and export are "
                                             "its positive and negative parts, netted within each interval")])
    negative = int(((long["delivered"] < 0) | (long["received"] < 0)).sum())
    report.ran("channels", [_finding("warning", "channels", f"{negative} intervals have negative delivered or "
                                     "received values -- is the sign convention right?")] if negative else [])

    # --- units to kW
    if spec.unit in ENERGY_UNITS:
        factor = ENERGY_UNITS[spec.unit] * 60.0 / minutes
    else:
        factor = POWER_UNITS[spec.unit]
    long["import_kw"] = long["delivered"].astype(float) * factor
    long["export_kw"] = long["received"].astype(float) * factor

    # --- one row per instant, on a regular grid
    duplicates = int(long["utc"].duplicated().sum())
    report.ran("duplicates", [_finding("warning", "duplicates", f"{duplicates} repeated intervals, averaged")]
               if duplicates else [])
    series = long.groupby("utc")[["import_kw", "export_kw"]].mean().sort_index()
    step = pd.Timedelta(minutes=minutes)
    grid = pd.date_range(series.index[0], series.index[-1], freq=step)
    off_grid = int((~series.index.isin(grid)).sum())
    if off_grid:
        report.ran("regular intervals", [_finding("warning", "regular intervals", f"{off_grid} readings fall between "
                                                  f"{minutes}-minute steps and were dropped")])
    series = series.reindex(grid)
    report.ran("regular intervals")

    # fill a gap only if the whole gap is short: interpolate(limit=n) would also fill
    # the first n steps of a long outage
    gaps = _runs(series["import_kw"].isna())
    short_gaps = [(start, n) for start, n in gaps if n <= spec.max_fill_intervals and start != series.index[0]
                  and series.index.get_loc(start) + n < len(series)]
    long_gaps = [g for g in gaps if g not in short_gaps]
    interpolated = series.interpolate(limit_area="inside")
    filled = series.copy()
    for start, n in short_gaps:
        rows = filled.index[filled.index.get_loc(start):filled.index.get_loc(start) + n]
        filled.loc[rows] = interpolated.loc[rows]
    short = sum(n for _, n in short_gaps)
    found = []
    if short:
        found.append(_finding("warning", "gaps", f"{short} missing intervals in gaps of up to "
                              f"{spec.max_fill_intervals} were interpolated"))
    if long_gaps:
        total = sum(length for _, length in long_gaps) * minutes / 60
        longest = max(long_gaps, key=lambda g: g[1])
        found.append(_finding("warning", "gaps", f"{len(long_gaps)} longer gaps ({total:g} h) left empty; longest "
                              f"{longest[1] * minutes / 60:g} h from {longest[0].tz_convert(spec.out_tz or spec.timestamp.tz)}"))
    report.ran("gaps", found)
    series = filled

    # --- to the output resolution, in real time
    resolution = spec.resolution_minutes or minutes
    if resolution < minutes:
        raise ValueError(f"resolution_minutes {resolution} is finer than the data's {minutes}-minute interval")
    if resolution > minutes:
        bins = series.resample(f"{resolution}min")
        counts = bins.count()["import_kw"]
        needed = np.ceil(spec.min_coverage * resolution / minutes)
        series = bins.mean().where(counts >= needed)

    # --- to the local clock, where every day has 24 hours
    tz = spec.out_tz or spec.timestamp.tz
    local = series.tz_convert(tz)
    naive_index = local.index.tz_localize(None)
    repeated = naive_index.duplicated(keep=False)
    local.index = naive_index
    local = local.groupby(level=0).mean()
    regular = pd.date_range(local.index[0], local.index[-1], freq=f"{resolution}min")
    skipped = regular.difference(local.index)
    local = local.reindex(regular)
    found = []
    if repeated.any():
        days = sorted({d.date() for d in naive_index[repeated]})
        found.append(_finding("warning", "daylight saving", f"November's repeated hour averaged into one on "
                              f"{', '.join(map(str, days))} (that day keeps 24 h; one hour of energy is dropped)"))
    if len(skipped):
        # only the skipped hour: long gaps left empty above stay empty
        local.loc[skipped] = local.interpolate(limit_area="inside").loc[skipped]
        days = sorted({d.date() for d in skipped})
        found.append(_finding("warning", "daylight saving", f"March's skipped hour interpolated on "
                              f"{', '.join(map(str, days))}"))
    report.ran("daylight saving", found)

    table = local.rename_axis("timestamp")
    table["net_kw"] = table["import_kw"] - table["export_kw"]
    report.extend(check_series(table["net_kw"], resolution, label="net_kw", import_kw=table["import_kw"]))
    dt = resolution / 60
    summary = {
        "first": str(table.index[0]), "last": str(table.index[-1]), "resolution_minutes": resolution,
        "source_interval_minutes": minutes, "rows": len(table),
        "import_kwh": round(float(table["import_kw"].sum() * dt), 1),
        "export_kwh": round(float(table["export_kw"].sum() * dt), 1),
        "peak_import_kw": round(float(table["import_kw"].max()), 2),
        "missing_rows": int(table["net_kw"].isna().sum()),
    }
    return IngestResult(table, report, summary)


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
    for start, length in _runs(imports.fillna(-1).eq(0)):
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
        elif 0 < peak < 0.05:
            found.append(_finding("warning", "units", f"{label}: never above {peak:.4f} kW -- kWh read as Wh, "
                                  "or MW as kW?"))
        if p99 > 0 and peak > 10 * p99 and peak > 5:
            found.append(_finding("warning", "spikes", f"{label}: peak {peak:,.1f} kW is over 10x the 99th "
                                  f"percentile ({p99:,.1f} kW) at {values.abs().idxmax()}"))
    report.ran("units and spikes", found)
    return report


def write_table(result: IngestResult, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.table.to_csv(path, float_format="%.4f")
    return path
