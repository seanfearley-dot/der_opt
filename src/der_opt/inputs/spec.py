"""Readers and validators for the input data spec, version 1.1
(docs/INPUT_DATA_SPEC.md): time series, premises and network files.

Each reader checks every rule and returns the data plus a CheckReport. Rule breaks
the spec rejects are `error` findings, and a reader raises SpecError listing all of
them at once (not just the first). Things worth a look are `warning`s, and expected
conversions -- such as daylight-saving days moving onto the local clock -- are `info`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.diagnostics import CheckReport, Finding
from der_opt.inputs.quality import check_series

SPEC_VERSION = "1.1"
CUSTOMER_CLASSES = ("residential", "commercial")
LOCAL_TZ = "America/Los_Angeles"

POWER_COLUMNS = ("load_kw", "pv_kw", "import_kw", "export_kw")
VALUE_COLUMNS = POWER_COLUMNS + ("ev_connected", "ev_driving_kwh", "import_price_per_kwh", "export_price_per_kwh")
GROSS, METERED = ("load_kw", "pv_kw"), ("import_kw", "export_kw")
INTERVALS = (5, 15, 30, 60)
ID = re.compile(r"^[A-Za-z0-9_-]+$")
ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")

PREMISE_NUMERIC = ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw",
                   "import_limit_kw", "export_limit_kw")
NETWORK_NUMERIC = ("rating_kw", "overload_cost_per_kwh")


class SpecError(ValueError):
    """A file breaks the spec; `report` has every finding."""

    def __init__(self, path, report: CheckReport):
        self.path, self.report = path, report
        lines = "\n".join(f"  - {f.message}" for f in report.errors)
        super().__init__(f"{path} doesn't meet the input data spec v{SPEC_VERSION}:\n{lines}")


def _err(check: str, message: str, premise: str | None = None) -> Finding:
    return Finding("error", check, message, None, premise)


def _warn(check: str, message: str, premise: str | None = None) -> Finding:
    return Finding("warning", check, message, None, premise)


def _info(check: str, message: str) -> Finding:
    return Finding("info", check, message)


def _read_csv(path: Path, report: CheckReport) -> pd.DataFrame | None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.ran("format", [_err("format", "not UTF-8 text (save the CSV as UTF-8)")])
        return None
    text = text.removeprefix("﻿")
    lines = text.splitlines()
    found = []
    if any(not line.strip() for line in lines[:-1]) or (lines and not lines[-1].strip() and len(lines) > 1):
        blanks = sum(1 for line in lines if not line.strip())
        found.append(_err("format", f"{blanks} blank lines; the file must be one header row and then data rows"))
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, skip_blank_lines=True, encoding="utf-8-sig")
    report.ran("format", found)
    return frame


def _check_names(frame: pd.DataFrame, known: tuple[str, ...], required: tuple[str, ...], report: CheckReport) -> bool:
    found = []
    lowered = {c.strip().lower(): c for c in frame.columns}
    for name in known:
        actual = lowered.get(name)
        if actual is not None and actual != name:
            found.append(_err("column names", f"rename column {actual!r} to {name!r} (exact lower-case names)"))
    missing = [name for name in required if name not in frame.columns]
    if missing:
        hint = " -- is the first row the header?" if "timestamp" in missing or "premise_id" in missing else ""
        found.append(_err("column names", f"missing required column(s) {missing}{hint}"))
    for column in frame.columns:
        if column not in known and re.search(r"_(kwh|wh|w|mw|mwh)$", column.lower()) and column.lower() not in known:
            found.append(_warn("column names", f"column {column!r} isn't in the spec and is ignored (power columns "
                               "are average kW, named *_kw)"))
    report.ran("column names", found)
    return not any(f.level == "error" for f in found)


def _numbers(frame: pd.DataFrame, columns, report: CheckReport, check="numbers") -> pd.DataFrame:
    """Parses value columns; empty cells become NaN, anything else unparseable is an error."""
    out = pd.DataFrame(index=frame.index)
    found = []
    for column in columns:
        if column not in frame:
            continue
        raw = frame[column].str.strip()
        values = pd.to_numeric(raw, errors="coerce")
        bad = raw.ne("") & values.isna()
        if bad.any():
            example = raw[bad].iloc[0]
            found.append(_err(check, f"{column}: {int(bad.sum())} cells aren't plain numbers (e.g. {example!r}); "
                              "use decimals like 1.25, with empty cells for missing"))
        negative = values < 0
        if negative.any():
            found.append(_err(check, f"{column}: {int(negative.sum())} negative values; values are never negative "
                              "(direction is in the column name)"))
        out[column] = values
    report.ran(check, found)
    return out


# --- time series --------------------------------------------------------------------


@dataclass
class TimeSeriesFile:
    path: Path
    interval_minutes: int
    premises: list[str] | None  # None for a shared file
    columns: list[str]
    data: pd.DataFrame  # timestamp (UTC), premise_id (if any), value columns
    report: CheckReport
    summary: dict = field(default_factory=dict)

    def local_series(self, column: str, premise_id: str | None = None, tz: str = LOCAL_TZ) -> pd.Series:
        """One column for one premise on the local clock (naive timestamps, 24 hours
        every day): November's repeated hour averaged into one, March's skipped hour
        interpolated. `net_kw` = import_kw - export_kw, or load_kw - pv_kw."""
        rows = self.data
        if self.premises is not None:
            if premise_id is None:
                raise ValueError(f"{self.path} has several premises; name one (premise_id)")
            rows = rows[rows["premise_id"] == premise_id]
            if rows.empty:
                raise ValueError(f"premise {premise_id!r} isn't in {self.path}")
        if column == "net_kw":
            if "import_kw" in rows and rows["import_kw"].notna().any():
                values = rows["import_kw"] - rows.get("export_kw", 0).fillna(0)
            else:
                values = rows["load_kw"] - rows.get("pv_kw", pd.Series(0.0, index=rows.index)).fillna(0)
        elif column in rows:
            values = rows[column]
        else:
            raise ValueError(f"{self.path} has no column {column!r}; it has {self.columns}")
        series = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(rows["timestamp"]))
        local = series.tz_convert(tz)
        local.index = local.index.tz_localize(None)
        local = local.groupby(level=0).mean()  # November's repeated hour
        regular = pd.date_range(local.index[0], local.index[-1], freq=f"{self.interval_minutes}min")
        skipped = regular.difference(local.index)
        local = local.reindex(regular)
        if len(skipped):  # March's skipped hour; empty cells elsewhere stay empty
            local.loc[skipped] = local.interpolate(limit_area="inside").loc[skipped]
        return local.rename(column)


@lru_cache(maxsize=32)
def _read_timeseries_cached(path: str, mtime: float) -> TimeSeriesFile:
    return _read_timeseries(Path(path))


def read_timeseries(path: str | Path, strict: bool = True) -> TimeSeriesFile:
    """Reads and validates a time series file. strict=True raises SpecError on any
    error finding; strict=False returns them in the report (for check-data)."""
    path = Path(path)
    result = _read_timeseries_cached(str(path.resolve()), path.stat().st_mtime) if strict else _read_timeseries(path)
    if strict and result.report.errors:
        raise SpecError(path, result.report)
    return result


def _read_timeseries(path: Path) -> TimeSeriesFile:
    report = CheckReport()
    frame = _read_csv(path, report)
    empty = TimeSeriesFile(path, 0, None, [], pd.DataFrame(), report)
    if frame is None or not _check_names(frame, ("timestamp", "premise_id") + VALUE_COLUMNS, ("timestamp",), report):
        return empty
    values_present = [c for c in VALUE_COLUMNS if c in frame]
    if not values_present:
        report.ran("columns", [_err("columns", f"no value columns; expected some of {list(VALUE_COLUMNS)}")])
        return empty
    shared = "premise_id" not in frame

    # timestamps: ISO 8601 with offset
    stamps = frame["timestamp"].str.strip()
    bad = ~stamps.str.match(ISO_WITH_OFFSET)
    found = []
    if bad.any():
        found.append(_err("timestamps", f"{int(bad.sum())} timestamps aren't ISO 8601 with a UTC offset (e.g. "
                          f"{stamps[bad].iloc[0]!r}; expected like 2026-07-13T00:00:00-07:00)"))
    report.ran("timestamps", found)
    if found:
        return empty
    utc = pd.to_datetime(stamps, utc=True, format="ISO8601")

    found = []
    if not shared:
        ids = frame["premise_id"].str.strip()
        bad_ids = sorted(set(ids[~ids.str.match(ID)]))
        if bad_ids:
            found.append(_err("premise ids", f"premise_id values with characters other than letters, digits, _ "
                              f"and -: {bad_ids[:5]}"))
    report.ran("premise ids", found)

    values = _numbers(frame, values_present, report)
    if "ev_connected" in values:
        odd = values["ev_connected"].notna() & ~values["ev_connected"].isin([0, 1])
        report.ran("ev_connected", [_err("ev_connected", f"{int(odd.sum())} ev_connected values aren't 0 or 1")]
                   if odd.any() else [])

    data = pd.concat([pd.DataFrame({"timestamp": utc}), values], axis=1)
    if not shared:
        data.insert(1, "premise_id", frame["premise_id"].str.strip())
    keys = ["premise_id", "timestamp"] if not shared else ["timestamp"]

    duplicated = data.duplicated(subset=keys)
    report.ran("duplicates", [_err("duplicates", f"{int(duplicated.sum())} repeated "
                                   f"{'premise_id + timestamp' if not shared else 'timestamps'}")]
               if duplicated.any() else [])
    sorted_ok = data[keys].equals(data.sort_values(keys)[keys])
    report.ran("order", [] if sorted_ok else [_warn("order", "rows aren't sorted by premise_id then timestamp "
                                                     "(loads fine)")])
    data = data.sort_values(keys).drop_duplicates(subset=keys).reset_index(drop=True)

    groups = data.groupby("premise_id") if not shared else [(None, data)]
    intervals, found, gross_vs_metered, missing_rows = set(), [], [], []
    for premise, rows in groups:
        steps = rows["timestamp"].diff().dropna()
        if steps.empty:
            continue
        step = steps.min()
        minutes = step / pd.Timedelta(minutes=1)
        intervals.add(minutes)
        if minutes not in INTERVALS:
            found.append(_err("interval", f"{minutes:g}-minute interval; the spec allows {list(INTERVALS)}", premise))
        elif (steps != step).any():
            gaps = int((steps > step).sum())
            missing_rows.append(_err("interval", f"{gaps} places where rows are missing (every interval needs a "
                                     "row; leave cells empty instead)", premise))
        if not shared:
            gross = any(rows[c].notna().any() for c in GROSS if c in rows)
            metered = any(rows[c].notna().any() for c in METERED if c in rows)
            if gross and metered:
                gross_vs_metered.append(_err("gross or metered", "has both gross (load_kw/pv_kw) and metered "
                                             "(import_kw/export_kw) values; use one form per premise", premise))
    if len(intervals) > 1:
        found.append(_err("interval", f"premises use different intervals {sorted(intervals)}; one per file"))
    report.ran("interval", found + missing_rows)
    report.ran("gross or metered", gross_vs_metered)

    # empty cells and data quality, per premise and column
    found = []
    interval = int(min(intervals)) if intervals else 60
    for premise, rows in groups:
        for column in values_present:
            if column not in rows:
                continue
            blanks = int(rows[column].isna().sum())
            if blanks and rows[column].notna().any():
                found.append(_warn("empty cells", f"{column}: {blanks} empty cells", premise))
    report.ran("empty cells", found)

    for premise, rows in groups:
        for column in (c for c in POWER_COLUMNS if c in rows and rows[c].notna().any()):
            series = pd.Series(rows[column].to_numpy(), index=pd.DatetimeIndex(rows["timestamp"]).tz_convert(None))
            sub = check_series(series, interval, label=column, home_peak_kw=100 if column != "pv_kw" else None,
                               check_zero_runs=column in ("load_kw", "import_kw"))
            for finding in sub.findings:
                report.findings.append(Finding(finding.level, finding.check, finding.message, None, premise))
            for name in sub.checks_run:
                report.ran(name)

    # daylight saving, for information
    offsets = data["timestamp"].dt.tz_convert(LOCAL_TZ).map(lambda t: t.utcoffset())
    notes = []
    if offsets.nunique() > 1:
        notes.append(_info("daylight saving", "the file crosses a daylight-saving change; on the local clock "
                           "November's repeated hour is averaged into one and March's skipped hour interpolated"))
    report.ran("daylight saving", notes)

    premises = None if shared else sorted(data["premise_id"].unique())
    summary = {"spec_version": SPEC_VERSION, "kind": "time series",
               "first": data["timestamp"].min().tz_convert(LOCAL_TZ).isoformat(),
               "last": data["timestamp"].max().tz_convert(LOCAL_TZ).isoformat(), "interval_minutes": interval,
               "premises": len(premises) if premises else "shared (no premise_id)", "columns": values_present}
    energy = {}
    for premise, rows in groups:
        dt = interval / 60
        energy[premise or "all"] = {c: {"kwh": round(float(rows[c].sum() * dt), 1), "peak_kw": round(float(rows[c].max()), 2)}
                                    for c in POWER_COLUMNS if c in rows and rows[c].notna().any()}
    summary["energy"] = energy
    return TimeSeriesFile(path, interval, premises, values_present, data, report, summary)


# --- premises -----------------------------------------------------------------------


@dataclass
class PremisesFile:
    path: Path
    table: pd.DataFrame  # indexed by premise_id; numeric columns parsed
    report: CheckReport


def read_premises(path: str | Path, network: NetworkFile | None = None, strict: bool = True) -> PremisesFile:
    path = Path(path)
    report = CheckReport()
    known = ("premise_id", "element_id", "customer_class", "tariff", "export_program", "baseline_territory",
             "ev_v2x") + PREMISE_NUMERIC
    frame = _read_csv(path, report)
    if frame is None or not _check_names(frame, known, ("premise_id",), report):
        if strict:
            raise SpecError(path, report)
        return PremisesFile(path, pd.DataFrame(), report)
    frame = frame.apply(lambda c: c.str.strip())
    found = []
    ids = frame["premise_id"]
    if ids.duplicated().any():
        found.append(_err("premise ids", f"repeated premise_id: {sorted(set(ids[ids.duplicated()]))[:5]}"))
    if (~ids.str.match(ID)).any():
        found.append(_err("premise ids", f"premise_id values with characters other than letters, digits, _ and -: "
                          f"{sorted(set(ids[~ids.str.match(ID)]))[:5]}"))
    report.ran("premise ids", found)
    numbers = _numbers(frame, PREMISE_NUMERIC, report)
    table = frame.drop(columns=[c for c in PREMISE_NUMERIC if c in frame]).join(numbers)

    found = []
    for a, b in (("battery_kwh", "battery_kw"), ("ev_battery_kwh", "ev_charger_kw")):
        has_a = table[a].notna() if a in table else pd.Series(False, index=table.index)
        has_b = table[b].notna() if b in table else pd.Series(False, index=table.index)
        for premise in table.loc[has_a ^ has_b, "premise_id"]:
            found.append(_err("device sizes", f"{a} and {b} go together; give both or neither", premise))
    if "ev_v2x" in table:
        odd = ~table["ev_v2x"].isin(["", "0", "1"])
        for premise in table.loc[odd, "premise_id"]:
            found.append(_err("device sizes", "ev_v2x must be 0, 1 or empty", premise))
    if "customer_class" in table:
        odd = ~table["customer_class"].isin(("",) + CUSTOMER_CLASSES)
        for premise in table.loc[odd, "premise_id"]:
            found.append(_err("billing", f"customer_class must be {' or '.join(CUSTOMER_CLASSES)} (or empty)", premise))
    if "export_program" in table:
        odd = ~table["export_program"].isin(["", "nbt", "nem2"])
        for premise in table.loc[odd, "premise_id"]:
            found.append(_err("billing", "export_program must be nbt, nem2 or empty", premise))
    report.ran("device sizes", [f for f in found if f.check == "device sizes"])
    report.ran("billing", [f for f in found if f.check == "billing"])

    found = []
    if network is not None and "element_id" in table:
        unknown = table.loc[(table["element_id"] != "") & ~table["element_id"].isin(network.table.index), "premise_id"]
        found = [_err("element ids", f"element_id {table.set_index('premise_id').loc[p, 'element_id']!r} isn't in "
                      f"{network.path.name}", p) for p in unknown]
    report.ran("element ids", found)
    if strict and report.errors:
        raise SpecError(path, report)
    return PremisesFile(path, table.set_index("premise_id"), report)


# --- network ------------------------------------------------------------------------


@dataclass
class NetworkFile:
    path: Path
    table: pd.DataFrame  # indexed by element_id
    report: CheckReport


def read_network(path: str | Path, strict: bool = True) -> NetworkFile:
    path = Path(path)
    report = CheckReport()
    frame = _read_csv(path, report)
    if frame is None or not _check_names(frame, ("element_id", "parent_id") + NETWORK_NUMERIC, ("element_id",), report):
        if strict:
            raise SpecError(path, report)
        return NetworkFile(path, pd.DataFrame(), report)
    frame = frame.apply(lambda c: c.str.strip())
    numbers = _numbers(frame, NETWORK_NUMERIC, report)
    table = frame.drop(columns=[c for c in NETWORK_NUMERIC if c in frame]).join(numbers)
    if "parent_id" not in table:
        table["parent_id"] = ""
    found = []
    ids = table["element_id"]
    if ids.duplicated().any():
        found.append(_err("element ids", f"repeated element_id: {sorted(set(ids[ids.duplicated()]))[:5]}"))
    unknown = table.loc[(table["parent_id"] != "") & ~table["parent_id"].isin(ids), "parent_id"]
    if len(unknown):
        found.append(_err("element ids", f"parent_id not among element_id: {sorted(set(unknown))[:5]}"))
    parents = dict(zip(table["element_id"], table["parent_id"]))
    for element in ids:
        seen, node = set(), element
        while parents.get(node):
            if node in seen:
                found.append(_err("cycles", f"the network loops back through {node!r}"))
                break
            seen.add(node)
            node = parents[node]
        else:
            continue
        break
    report.ran("element ids", [f for f in found if f.check == "element ids"])
    report.ran("cycles", [f for f in found if f.check == "cycles"])
    if strict and report.errors:
        raise SpecError(path, report)
    return NetworkFile(path, table.set_index("element_id"), report)


# --- any file -----------------------------------------------------------------------


def detect_kind(path: str | Path) -> str:
    header = Path(path).read_text(encoding="utf-8").removeprefix("﻿").splitlines()[0]
    names = {c.strip().lower() for c in header.split(",")}
    if "timestamp" in names:
        return "time series"
    if "premise_id" in names:
        return "premises"
    if "element_id" in names:
        return "network"
    return "unknown"


def check_file(path: str | Path, premises: str | Path | None = None, network: str | Path | None = None) -> tuple[str, CheckReport, dict]:
    """Validates any spec file without raising; cross-checks IDs against the premises
    and network files when given."""
    kind = detect_kind(path)
    net = read_network(network, strict=False) if network else None
    if kind == "time series":
        result = read_timeseries(path, strict=False)
        report, summary = result.report, result.summary
        if premises and result.premises:
            known = read_premises(premises, net, strict=False).table.index
            missing = sorted(set(result.premises) - set(known))
            report.ran("premise ids", [_err("premise ids", f"premise_id not in {Path(premises).name}: {missing[:5]}")]
                       if missing else [])
            table = read_premises(premises, net, strict=False).table
            if "pv_kw_ac" in table and "pv_kw" in result.columns:
                found = []
                for premise, rows in result.data.groupby("premise_id"):
                    rating = table["pv_kw_ac"].get(premise, np.nan)
                    if pd.notna(rating) and rows["pv_kw"].max() > rating * 1.001:
                        found.append(_warn("pv vs rating", f"pv_kw reaches {rows['pv_kw'].max():.2f}, above "
                                           f"pv_kw_ac {rating:g}", premise))
                report.ran("pv vs rating", found)
    elif kind == "premises":
        result = read_premises(path, net, strict=False)
        report = result.report
        summary = {"spec_version": SPEC_VERSION, "kind": "premises", "premises": len(result.table)}
    elif kind == "network":
        result = read_network(path, strict=False)
        report = result.report
        summary = {"spec_version": SPEC_VERSION, "kind": "network", "elements": len(result.table)}
    else:
        report = CheckReport()
        report.ran("kind", [_err("kind", "first row names none of timestamp, premise_id or element_id; is the "
                                 "header the first row?")])
        summary = {"spec_version": SPEC_VERSION, "kind": "unknown"}
    return kind, report, summary
