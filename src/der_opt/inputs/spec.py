"""Readers and validators for the input data spec, version 2.0
(docs/INPUT_DATA_SPEC.md): time series, premises, devices and network files.

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

SPEC_VERSION = "2.0"
CUSTOMER_CLASSES = ("residential", "commercial")
LOCAL_TZ = "America/Los_Angeles"

POWER_COLUMNS = ("load_kw", "pv_kw", "import_kw", "export_kw")
VALUE_COLUMNS = POWER_COLUMNS + ("ev_connected", "ev_driving_kwh", "import_price_per_kwh", "export_price_per_kwh",
                                 "outdoor_temp_c", "flexible_kw")
SIGNED_COLUMNS = ("outdoor_temp_c",)  # may be negative
KEY_COLUMNS = ("premise_id", "device_id", "weather_station")
GROSS, METERED = ("load_kw", "pv_kw"), ("import_kw", "export_kw")
INTERVALS = (5, 15, 30, 60)
ID = re.compile(r"^[A-Za-z0-9_-]+$")
ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")

V11_DEVICE_COLUMNS = ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw", "ev_v2x")
PREMISE_NUMERIC = ("pv_kw_ac", "battery_kwh", "battery_kw", "ev_battery_kwh", "ev_charger_kw",
                   "import_limit_kw", "export_limit_kw", "poi_limit_kw", "envelope_weight", "income_tier")
# premise settings a premises file can carry as columns (2.0), and the values each takes
PREMISE_CHOICES = {
    "kind": ("premise", "background"),
    "participation": ("participant", "non_participant"),
    "baseline_behavior": ("own_bill", "rule_based"),
    "response": ("optimized", "rules"),
    "exclusive_import_export": ("0", "1"),
    "load": ("none",),
    "baseline_code": ("basic", "all_electric"),
}
PREMISE_COLUMNS = ("premise_id", "element_id", "customer_class", "tariff", "export_program", "baseline_territory",
                   *PREMISE_NUMERIC, *PREMISE_CHOICES, "ev_v2x")
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


def _numbers(frame: pd.DataFrame, columns, report: CheckReport, check="numbers", signed=SIGNED_COLUMNS) -> pd.DataFrame:
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
        if negative.any() and column not in signed:
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
    data: pd.DataFrame  # timestamp (UTC), key columns (premise_id, device_id or weather_station), values
    report: CheckReport
    summary: dict = field(default_factory=dict)
    stations: list[str] | None = None  # a weather file keyed by weather_station

    def rows_for(self, premise_id: str | None = None, device_id: str | None = None,
                 station: str | None = None, column: str | None = None) -> pd.DataFrame:
        """The rows holding one premise's (or device's, or station's) series. Rows with
        an empty device_id are the premise's own; a device's rows have its device_id."""
        rows = self.data
        if self.stations is not None:
            if station is None:
                raise ValueError(f"{self.path} is keyed by weather_station; name one")
            return rows[rows["weather_station"] == station]
        if self.premises is None:
            return rows
        if premise_id is None:
            raise ValueError(f"{self.path} has several premises; name one (premise_id)")
        rows = rows[rows["premise_id"] == premise_id]
        if "device_id" not in rows:
            return rows
        if device_id is not None:
            mine = rows[rows["device_id"] == device_id]
            if not mine.empty and (column is None or (column in mine and mine[column].notna().any())):
                return mine
        own = rows[rows["device_id"] == ""]
        if not own.empty and (column is None or (column in own and own[column].notna().any())):
            return own
        if column is not None and column in rows:  # one device with this column: matched by type
            holders = rows.loc[rows[column].notna(), "device_id"].unique()
            if len(holders) == 1:
                return rows[rows["device_id"] == holders[0]]
        return own

    def has(self, column: str, premise_id: str | None = None, device_id: str | None = None,
            station: str | None = None) -> bool:
        if column not in self.columns and column != "net_kw":
            return False
        if self.premises is not None and premise_id not in self.premises:
            return False
        if self.stations is not None and station not in self.stations:
            return False
        rows = self.rows_for(premise_id, device_id, station, column if column != "net_kw" else None)
        return column in rows and bool(rows[column].notna().any())

    def local_series(self, column: str, premise_id: str | None = None, tz: str = LOCAL_TZ,
                     device_id: str | None = None, station: str | None = None) -> pd.Series:
        """One column for one premise (or device, or weather station) on the local clock
        (naive timestamps, 24 hours every day): November's repeated hour averaged into
        one, March's skipped hour interpolated. `net_kw` = import_kw - export_kw, or
        load_kw - pv_kw."""
        if self.premises is not None and premise_id is not None and premise_id not in self.premises:
            raise ValueError(f"premise {premise_id!r} isn't in {self.path}")
        rows = self.rows_for(premise_id, device_id, station, column if column != "net_kw" else None)
        if rows.empty:
            raise ValueError(f"no rows for {premise_id or station!r} in {self.path}")
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
    if frame is None or not _check_names(frame, ("timestamp", *KEY_COLUMNS) + VALUE_COLUMNS, ("timestamp",), report):
        return empty
    values_present = [c for c in VALUE_COLUMNS if c in frame]
    if not values_present:
        report.ran("columns", [_err("columns", f"no value columns; expected some of {list(VALUE_COLUMNS)}")])
        return empty
    if "weather_station" in frame and "premise_id" in frame:
        report.ran("columns", [_err("columns", "a file is keyed by premise_id or by weather_station, not both")])
        return empty
    by_station = "weather_station" in frame
    if "device_id" in frame and "premise_id" not in frame:
        report.ran("columns", [_err("columns", "device_id needs premise_id")])
        return empty
    shared = "premise_id" not in frame and not by_station

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
    keys_present = [k for k in KEY_COLUMNS if k in frame]
    for key in keys_present:
        ids = frame[key].str.strip()
        allowed = ids.str.match(ID) | ((ids == "") if key == "device_id" else False)
        bad_ids = sorted(set(ids[~allowed]))
        if bad_ids:
            found.append(_err("premise ids", f"{key} values with characters other than letters, digits, _ "
                              f"and -: {bad_ids[:5]}"))
    report.ran("premise ids", found)

    values = _numbers(frame, values_present, report)
    if "ev_connected" in values:
        odd = values["ev_connected"].notna() & ~values["ev_connected"].isin([0, 1])
        report.ran("ev_connected", [_err("ev_connected", f"{int(odd.sum())} ev_connected values aren't 0 or 1")]
                   if odd.any() else [])

    data = pd.concat([pd.DataFrame({"timestamp": utc}), values], axis=1)
    for position, key in enumerate(keys_present, start=1):
        data.insert(position, key, frame[key].str.strip())
    keys = [*keys_present, "timestamp"]

    duplicated = data.duplicated(subset=keys)
    report.ran("duplicates", [_err("duplicates", f"{int(duplicated.sum())} repeated {' + '.join(keys)}")]
               if duplicated.any() else [])
    sorted_ok = data[keys].equals(data.sort_values(keys)[keys])
    report.ran("order", [] if sorted_ok else [_warn("order", "rows aren't sorted by premise_id then timestamp "
                                                     "(loads fine)")])
    data = data.sort_values(keys).drop_duplicates(subset=keys).reset_index(drop=True)

    series_keys = list(keys_present)

    def _label(key) -> str:
        parts = key if isinstance(key, tuple) else (key,)
        return "/".join(str(part) for part in parts if part)

    groups = ([(_label(key), rows) for key, rows in data.groupby(series_keys[0] if len(series_keys) == 1 else series_keys)]
              if series_keys else [(None, data)])
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
        if not shared and not by_station:
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

    premises = sorted(data["premise_id"].unique()) if "premise_id" in data else None
    stations = sorted(data["weather_station"].unique()) if by_station else None
    summary = {"spec_version": SPEC_VERSION, "kind": "time series",
               "first": data["timestamp"].min().tz_convert(LOCAL_TZ).isoformat(),
               "last": data["timestamp"].max().tz_convert(LOCAL_TZ).isoformat(), "interval_minutes": interval,
               "premises": len(premises) if premises else (f"{len(stations)} weather stations" if stations
                                                           else "shared (no premise_id)"), "columns": values_present}
    energy = {}
    for premise, rows in groups:
        dt = interval / 60
        energy[premise or "all"] = {c: {"kwh": round(float(rows[c].sum() * dt), 1), "peak_kw": round(float(rows[c].max()), 2)}
                                    for c in POWER_COLUMNS if c in rows and rows[c].notna().any()}
    summary["energy"] = energy
    return TimeSeriesFile(path, interval, premises, values_present, data, report, summary, stations)


# --- premises -----------------------------------------------------------------------


@dataclass
class PremisesFile:
    path: Path
    table: pd.DataFrame  # indexed by premise_id; numeric columns parsed; tags kept as text
    report: CheckReport

    @property
    def v11_device_columns(self) -> list[str]:
        """The 1.1 device columns this file has values in (read as devices, with a note)."""
        return [c for c in V11_DEVICE_COLUMNS if c in self.table and (self.table[c].astype(str).str.strip()
                                                                      .replace("nan", "") != "").any()]

    @property
    def tags(self) -> list[str]:
        return [c for c in self.table.columns if c not in PREMISE_COLUMNS]


def read_premises(path: str | Path, network: NetworkFile | None = None, strict: bool = True) -> PremisesFile:
    path = Path(path)
    report = CheckReport()
    known = PREMISE_COLUMNS
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
    for column, choices in PREMISE_CHOICES.items():
        if column in table:
            odd = ~table[column].isin(("", *choices))
            for premise in table.loc[odd, "premise_id"]:
                found.append(_err("settings", f"{column} must be {', '.join(choices)} (or empty)", premise))
    if "response" in table:
        baseline = table["baseline_behavior"] if "baseline_behavior" in table else pd.Series("", index=table.index)
        clash = (table["response"] == "rules") & (baseline != "rule_based")
        for premise in table.loc[clash, "premise_id"]:
            found.append(_err("settings", "response: rules keeps the baseline rules, so it needs baseline_behavior: "
                              "rule_based", premise))
    if "kind" in table:
        background = table["kind"] == "background"
        devices = [c for c in V11_DEVICE_COLUMNS if c in table]
        for premise in table.loc[background, "premise_id"]:
            row = table.set_index("premise_id").loc[premise]
            if any(str(row[c]).strip() not in ("", "nan") for c in devices):
                found.append(_err("settings", "a background row has no devices", premise))
    report.ran("device sizes", [f for f in found if f.check == "device sizes"])
    report.ran("billing", [f for f in found if f.check == "billing"])
    report.ran("settings", [f for f in found if f.check == "settings"])

    found = []
    if network is not None and "element_id" in table:
        unknown = table.loc[(table["element_id"] != "") & ~table["element_id"].isin(network.table.index), "premise_id"]
        found = [_err("element ids", f"element_id {table.set_index('premise_id').loc[p, 'element_id']!r} isn't in "
                      f"{network.path.name}", p) for p in unknown]
    report.ran("element ids", found)
    if strict and report.errors:
        raise SpecError(path, report)
    return PremisesFile(path, table.set_index("premise_id"), report)


# --- devices (2.0) ------------------------------------------------------------------

DEVICE_TYPES = {"solar": "solar", "battery": "battery", "ev": "ev_v2x", "thermostat": "thermostat",
                "flexible_load": "curtailable_load"}
DEVICE_COLUMNS = ("premise_id", "device_id", "type", "template", "kw", "kwh", "v2x", "installed_cost", "annual_om",
                  "charge_sources")
DEVICE_NUMERIC = ("kw", "kwh", "installed_cost", "annual_om")
# parameters each device type's rows may carry as columns
SERIES_PARAMS = {"available_kw", "connected", "driving_kwh", "ambient_temp_c", "baseline_load_kw", "departure_period"}


def device_parameters(kind: str) -> list[str]:
    """The parameters a device type takes (beyond its name and time grid)."""
    import inspect

    from der_opt.scenarios.loader import DEVICE_REGISTRY

    cls = DEVICE_REGISTRY[DEVICE_TYPES.get(kind, kind)]
    return [p for p in inspect.signature(cls.__init__).parameters if p not in ("self", "name", "time_grid")]


@dataclass
class DevicesFile:
    path: Path
    table: pd.DataFrame  # one row per device, with `row` (its line in the file)
    report: CheckReport

    def for_premise(self, premise_id: str) -> pd.DataFrame:
        return self.table[self.table["premise_id"] == premise_id]


def read_devices(path: str | Path, premises: PremisesFile | None = None, strict: bool = True) -> DevicesFile:
    """Reads and validates a devices file (2.0)."""
    path = Path(path)
    report = CheckReport()
    frame = _read_csv(path, report)
    parameters = {kind: device_parameters(kind) for kind in DEVICE_TYPES}
    every = sorted({p for ps in parameters.values() for p in ps} - SERIES_PARAMS)
    if frame is None or not _check_names(frame, DEVICE_COLUMNS + tuple(every), ("premise_id", "device_id", "type"),
                                         report):
        if strict:
            raise SpecError(path, report)
        return DevicesFile(path, pd.DataFrame(), report)
    frame = frame.apply(lambda c: c.str.strip())
    frame.insert(0, "row", range(2, len(frame) + 2))  # line 1 is the header
    found = []
    for key in ("premise_id", "device_id"):
        bad = sorted(set(frame.loc[~frame[key].str.match(ID), key]))
        if bad:
            found.append(_err("device ids", f"{key} values with characters other than letters, digits, _ and -: {bad[:5]}"))
    repeated = frame.duplicated(subset=["premise_id", "device_id"])
    if repeated.any():
        pairs = sorted(set(frame.loc[repeated, "premise_id"] + "/" + frame.loc[repeated, "device_id"]))
        found.append(_err("device ids", f"repeated premise_id + device_id: {pairs[:5]}"))
    report.ran("device ids", found)

    found = []
    odd_type = ~frame["type"].isin(DEVICE_TYPES)
    for _, r in frame[odd_type].iterrows():
        found.append(_err("types", f"row {r['row']}: type {r['type']!r} isn't one of {', '.join(DEVICE_TYPES)}",
                          r["premise_id"]))
    report.ran("types", found)

    extra = [c for c in frame.columns if c not in DEVICE_COLUMNS and c != "row"]
    unknown = [c for c in extra if c not in every]
    found = [_err("columns", f"{c!r} isn't a device parameter; device parameters are {every}") for c in unknown]
    for _, r in frame[~odd_type].iterrows():
        for c in extra:
            if c in every and r[c] != "" and c not in parameters[r["type"]]:
                found.append(_err("columns", f"row {r['row']}: a {r['type']} has no parameter {c!r} "
                                  f"(it takes {parameters[r['type']]})", r["premise_id"]))
    if "v2x" in frame:
        odd = ~frame["v2x"].isin(["", "0", "1"])
        for _, r in frame[odd].iterrows():
            found.append(_err("columns", f"row {r['row']}: v2x must be 0, 1 or empty", r["premise_id"]))
        for _, r in frame[(frame["v2x"] == "1") & (frame["type"] != "ev")].iterrows():
            found.append(_err("columns", f"row {r['row']}: v2x is for EVs", r["premise_id"]))
    report.ran("columns", found)

    numbers = _numbers(frame, [c for c in (*DEVICE_NUMERIC, *extra) if c in every or c in DEVICE_NUMERIC], report)
    table = frame.drop(columns=list(numbers.columns)).join(numbers)

    found = []
    if premises is not None and not premises.table.empty:
        unknown_premises = sorted(set(table["premise_id"]) - set(premises.table.index))
        if unknown_premises:
            found.append(_err("premise ids", f"premise_id not in {premises.path.name}: {unknown_premises[:5]}"))
        if "kind" in premises.table:
            background = set(premises.table.index[premises.table["kind"] == "background"])
            on_background = sorted(set(table["premise_id"]) & background)
            if on_background:
                found.append(_err("premise ids", f"devices on background rows (which have none): {on_background[:5]}"))
        if premises.v11_device_columns:
            found.append(_err("premise ids", f"{premises.path.name} still has v1.1 device columns "
                              f"{premises.v11_device_columns}; with a devices file, remove them "
                              "(`der-opt convert premises` does it)"))
    report.ran("premise ids", found)
    if strict and report.errors:
        raise SpecError(path, report)
    return DevicesFile(path, table, report)


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
    if {"premise_id", "device_id", "type"} <= names:
        return "devices"
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
        if "kind" in result.table:
            summary["background rows"] = int((result.table["kind"] == "background").sum())
        if result.tags:
            summary["tags"] = result.tags
        if result.v11_device_columns:
            report.ran("v1.1 device columns", [_warn("v1.1 device columns", f"{result.v11_device_columns} are "
                                                     "the 1.1 form, read as devices for one release; `der-opt convert "
                                                     "premises` writes a 2.0 devices file")])
    elif kind == "devices":
        known = read_premises(premises, net, strict=False) if premises else None
        result = read_devices(path, known, strict=False)
        report = result.report
        counts = result.table["type"].value_counts().to_dict() if not result.table.empty else {}
        summary = {"spec_version": SPEC_VERSION, "kind": "devices", "devices": len(result.table),
                   "by type": counts}
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
