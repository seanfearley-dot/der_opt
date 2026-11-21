"""Avoided cost sets (docs/AVOIDED_COST_SPEC.md v1.0): one folder with manifest.yaml,
hourly.csv (every hour of every year, ISO 8601 with offset, components as columns) and
capacity.csv ($/kW-year by year and capacity component, with monthly shares).

An ACC vintage and internal curves use the same format. Components are classified
`energy` or `capacity`; DR capacity is valued only from capacity.csv, never from the
hourly capacity-class components.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from der_opt.diagnostics import CheckReport, Finding
from der_opt.inputs.spec import ISO_WITH_OFFSET

CAPACITY_COMPONENTS = ("generation", "transmission", "distribution")
MONTHS = [f"m{m}" for m in range(1, 13)]
PLAUSIBLE = (-1.0, 10.0)  # $/kWh


class ComponentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kind: Literal["energy", "capacity"] = Field(alias="class")
    capacity_component: Literal["generation", "transmission", "distribution"] | None = None
    sct_only: bool = False  # a societal value only the SCT includes


class Dollars(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis: Literal["nominal", "real"]
    base_year: int


class Extend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["escalate"]
    rate_per_year: float


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_id: str
    source: str
    location: dict = Field(default_factory=dict)
    dollars: Dollars
    at: Literal["meter", "generator"] = "meter"
    years: list[int]
    components: dict[str, ComponentSpec]
    emissions: str | None = None  # column of tCO2/MWh
    extend: Extend | None = None
    tz: str = "America/Los_Angeles"


class SetError(ValueError):
    def __init__(self, path, report: CheckReport):
        self.path, self.report = path, report
        lines = "\n".join(f"  - {f.message}" for f in report.errors)
        super().__init__(f"{path} isn't a valid avoided cost set (docs/AVOIDED_COST_SPEC.md):\n{lines}")


@dataclass
class AvoidedCostSet:
    path: Path
    manifest: Manifest
    hourly: pd.DataFrame  # UTC-indexed; component and emissions columns
    capacity: pd.DataFrame  # year, capacity_component, price_per_kw_year, m1..m12
    report: CheckReport

    @property
    def first_year(self) -> int:
        return self.manifest.years[0]

    @property
    def last_year(self) -> int:
        return self.manifest.years[1]

    def components_of(self, kind: str, include_sct_only: bool = True) -> list[str]:
        return [name for name, c in self.manifest.components.items()
                if c.kind == kind and (include_sct_only or not c.sct_only)]

    def _escalation(self, year: int) -> tuple[int, float]:
        """The set year to use for `year`, and the factor to scale it by."""
        if self.first_year <= year <= self.last_year:
            return year, 1.0
        if year > self.last_year and self.manifest.extend is not None:
            return self.last_year, (1 + self.manifest.extend.rate_per_year) ** (year - self.last_year)
        raise ValueError(f"avoided cost set {self.manifest.set_id} covers {self.first_year}-{self.last_year}, not "
                         f"{year}; add years, or declare `extend` in its manifest")

    def year_local(self, year: int, columns: list[str] | None = None) -> pd.DataFrame:
        """A year's hourly values on the local clock (naive timestamps, 24 hours every
        day: November's repeated hour averaged, March's skipped hour interpolated), as
        der-opt runs see time. Beyond the set, the last year escalated (if declared),
        matched by month, day and hour."""
        source_year, factor = self._escalation(year)
        tz = self.manifest.tz
        start = pd.Timestamp(f"{source_year}-01-01", tz=tz).tz_convert("UTC")
        end = pd.Timestamp(f"{source_year + 1}-01-01", tz=tz).tz_convert("UTC")
        rows = self.hourly.loc[(self.hourly.index >= start) & (self.hourly.index < end), columns or list(self.hourly.columns)]
        local = rows.copy()
        local.index = rows.index.tz_convert(tz).tz_localize(None)
        local = local.groupby(level=0).mean()
        regular = pd.date_range(f"{source_year}-01-01", f"{source_year + 1}-01-01", freq="h", inclusive="left")
        skipped = regular.difference(local.index)
        local = local.reindex(regular)
        if len(skipped):
            local.loc[skipped] = local.interpolate(limit_area="inside").loc[skipped]
        if source_year != year:
            local = _to_year(local, year)
        emissions = self.manifest.emissions
        scaled = local.mul(factor)
        if emissions in scaled:
            scaled[emissions] = local[emissions]  # emissions intensity isn't escalated
        return scaled

    def capacity_price(self, year: int, component: str) -> tuple[float, np.ndarray] | None:
        """($/kW-year, 12 monthly shares) for a capacity component, or None if the set
        has no price for it."""
        source_year, factor = self._escalation(year)
        rows = self.capacity[(self.capacity["year"] == source_year) & (self.capacity["capacity_component"] == component)]
        if rows.empty:
            return None
        row = rows.iloc[0]
        return float(row["price_per_kw_year"]) * factor, row[MONTHS].to_numpy(dtype=float)


def _to_year(frame: pd.DataFrame, year: int) -> pd.DataFrame:
    """Re-date hourly rows to another year by month, day and hour; Feb 29 dropped or
    filled from Feb 28 as the years require."""
    target = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", inclusive="left")
    keyed = frame.copy()
    keyed.index = pd.MultiIndex.from_arrays([frame.index.month, frame.index.day, frame.index.hour])
    keys = pd.MultiIndex.from_arrays([target.month, np.where((target.month == 2) & (target.day == 29), 28, target.day),
                                      target.hour])
    out = keyed.reindex(keys)
    out.index = target
    return out


def _err(check, message):
    return Finding("error", check, message)


def read_set(path: str | Path, strict: bool = True) -> AvoidedCostSet:
    path = Path(path)
    report = CheckReport()

    def fail():
        if strict:
            raise SetError(path, report)
        return AvoidedCostSet(path, None, pd.DataFrame(), pd.DataFrame(), report)

    files = {name: path / name for name in ("manifest.yaml", "hourly.csv", "capacity.csv")}
    missing = [name for name, f in files.items() if not f.exists()]
    report.ran("files", [_err("files", f"missing {missing}")] if missing else [])
    if missing:
        return fail()
    try:
        manifest = Manifest.model_validate(yaml.safe_load(files["manifest.yaml"].read_text(encoding="utf-8")))
    except ValidationError as exc:
        report.ran("manifest", [_err("manifest", f"manifest.yaml: {e['loc']}: {e['msg']}") for e in exc.errors()])
        return fail()
    found = []
    if len(manifest.years) != 2 or manifest.years[0] > manifest.years[1]:
        found.append(_err("manifest", "years must be [first, last]"))
    for name, c in manifest.components.items():
        if c.kind == "capacity" and c.capacity_component is None:
            found.append(_err("manifest", f"capacity component {name!r} needs capacity_component"))
    report.ran("manifest", found)
    if found:
        return fail()

    # --- hourly.csv
    frame = pd.read_csv(files["hourly.csv"], dtype={"timestamp": str})
    found = []
    expected_cols = list(manifest.components) + ([manifest.emissions] if manifest.emissions else [])
    missing_cols = [c for c in ["timestamp"] + expected_cols if c not in frame]
    unclassified = [c for c in frame.columns if c != "timestamp" and c not in expected_cols]
    if missing_cols:
        found.append(_err("hourly columns", f"hourly.csv lacks column(s) {missing_cols}"))
    if unclassified:
        found.append(_err("hourly columns", f"hourly.csv column(s) {unclassified} have no class in the manifest"))
    report.ran("hourly columns", found)
    if found:
        return fail()
    stamps = frame["timestamp"].str.strip()
    bad = ~stamps.str.match(ISO_WITH_OFFSET)
    if bad.any():
        report.ran("hourly timestamps", [_err("hourly timestamps", f"{int(bad.sum())} timestamps aren't ISO 8601 with a "
                                         f"UTC offset (e.g. {stamps[bad].iloc[0]!r})")])
        return fail()
    utc = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True, format="ISO8601"))
    first, last = manifest.years
    want = pd.date_range(pd.Timestamp(f"{first}-01-01", tz=manifest.tz).tz_convert("UTC"),
                         pd.Timestamp(f"{last + 1}-01-01", tz=manifest.tz).tz_convert("UTC"), freq="h", inclusive="left")
    found = []
    if utc.has_duplicates:
        found.append(_err("hourly coverage", f"{int(utc.duplicated().sum())} repeated hours"))
    missing_hours = want.difference(utc)
    extra = utc.difference(want)
    if len(missing_hours):
        found.append(_err("hourly coverage", f"{len(missing_hours)} hours of {first}-{last} missing (first "
                          f"{missing_hours[0].tz_convert(manifest.tz)})"))
    if len(extra):
        found.append(_err("hourly coverage", f"{len(extra)} hours outside {first}-{last}"))
    report.ran("hourly coverage", found)
    values = frame[expected_cols].apply(pd.to_numeric, errors="coerce")
    found = []
    for column in expected_cols:
        blank = int(values[column].isna().sum())
        if blank:
            found.append(_err("hourly values", f"{column}: {blank} empty or non-numeric values"))
    report.ran("hourly values", found)
    found = []
    for column in manifest.components:
        low, high = values[column].min(), values[column].max()
        if low < PLAUSIBLE[0] or high > PLAUSIBLE[1]:
            found.append(Finding("warning", "hourly ranges", f"{column} runs {low:.3f} to {high:.3f} $/kWh, outside "
                                 f"{PLAUSIBLE[0]:g} to {PLAUSIBLE[1]:g} -- $/MWh not divided by 1000?"))
    report.ran("hourly ranges", found)
    hourly = values.set_index(utc).sort_index()

    # --- capacity.csv
    capacity = pd.read_csv(files["capacity.csv"])
    found = []
    needed_cols = ["year", "capacity_component", "price_per_kw_year"] + MONTHS
    lacking = [c for c in needed_cols if c not in capacity]
    if lacking:
        found.append(_err("capacity", f"capacity.csv lacks column(s) {lacking}"))
    else:
        odd = capacity[~capacity["capacity_component"].isin(CAPACITY_COMPONENTS)]
        if len(odd):
            found.append(_err("capacity", f"capacity_component must be one of {list(CAPACITY_COMPONENTS)}"))
        sums = capacity[MONTHS].sum(axis=1)
        if ((sums - 1).abs() > 1e-6).any():
            found.append(_err("capacity", f"{int(((sums - 1).abs() > 1e-6).sum())} rows' monthly shares don't sum to 1"))
        if (capacity[MONTHS] < 0).any().any():
            found.append(_err("capacity", "negative monthly shares"))
        if (capacity["price_per_kw_year"] < 0).any():
            found.append(_err("capacity", "negative price_per_kw_year"))
        used = {c.capacity_component for c in manifest.components.values() if c.kind == "capacity"}
        for component in sorted(used):
            years = set(capacity.loc[capacity["capacity_component"] == component, "year"])
            gap = [y for y in range(first, last + 1) if y not in years]
            if gap:
                found.append(_err("capacity", f"{component}: no price for {gap[:5]}{'...' if len(gap) > 5 else ''}"))
        if capacity.duplicated(subset=["year", "capacity_component"]).any():
            found.append(_err("capacity", "repeated year + capacity_component rows"))
    report.ran("capacity", found)

    result = AvoidedCostSet(path, manifest, hourly, capacity, report)
    if strict and report.errors:
        raise SetError(path, report)
    return result


def describe(acs: AvoidedCostSet) -> dict:
    m = acs.manifest
    if m is None:
        return {"kind": "avoided cost set"}
    return {
        "kind": "avoided cost set", "set_id": m.set_id, "source": m.source,
        "dollars": f"{m.dollars.basis} ({m.dollars.base_year})", "years": f"{m.years[0]}-{m.years[1]}",
        "energy components": acs.components_of("energy"), "capacity components": acs.components_of("capacity"),
        "extend": f"escalate {m.extend.rate_per_year:.1%}/yr" if m.extend else "no (years outside are an error)",
    }
