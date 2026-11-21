"""An EV's daily pattern (docs/EV_PATTERN_SPEC.md v1.0): when it leaves and comes back on
weekdays and weekends and how far it goes, turned into the per-step inputs V2XVehicle
takes: `connected`, `departure_period` and `driving_kwh`.

Times are on the run's local clock (24 hours every day). A step the car is away for
any part of counts as away, so the run never counts charging the car couldn't do.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from der_opt.time_grid import TimeGrid

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
CLASHES = ("connected", "departure_period", "driving_kwh")


class DayPattern(BaseModel):
    depart: str
    arrive: str
    miles: float = Field(ge=0)

    @field_validator("depart", "arrive")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(str(value)):
            raise ValueError(f"{value!r} isn't a time as HH:MM (e.g. 07:30)")
        return value

    @model_validator(mode="after")
    def _different(self):
        if self.depart == self.arrive:
            raise ValueError(f"depart and arrive are both {self.depart}; a trip needs a length")
        return self

    def times(self) -> tuple[time, time]:
        return time.fromisoformat(self.depart), time.fromisoformat(self.arrive)


class EVPattern(BaseModel):
    weekday: DayPattern | Literal["home"]
    weekend: DayPattern | Literal["home"]
    kwh_per_mile: float = Field(gt=0)
    holidays: Literal["weekend", "weekday"] = "weekend"

    def for_day(self, day: date) -> tuple[str, DayPattern | Literal["home"]]:
        from der_opt.billing.holidays import pge_holidays

        if day.weekday() >= 5:
            kind = "weekend"
        elif day in pge_holidays(day.year):
            kind = self.holidays
        else:
            kind = "weekday"
        return kind, getattr(self, kind)


@dataclass
class Trip:
    day: date
    kind: str
    depart: datetime
    arrive: datetime
    miles: float
    kwh: float


@dataclass
class ExpandedPattern:
    connected: np.ndarray
    driving_kwh: np.ndarray
    departure_period: list[int]
    trips: list[Trip]
    notes: list[tuple[str, str]] = field(default_factory=list)  # (level, message)

    def summary(self, time_grid: TimeGrid, kwh_per_mile: float) -> str:
        inside = [t for t in self.trips if t.kwh > 0 or t.miles > 0]
        start = time_grid.start
        miles = kwh = 0.0
        for trip in inside:  # the part of each trip inside the run
            share = _share_inside(trip, start, time_grid)
            miles, kwh = miles + trip.miles * share, kwh + trip.kwh * share
        hours = {}
        dt = time_grid.dt_hours
        for kind in ("weekday", "weekend"):
            days = [d for d in _run_days(time_grid) if _kind_of(self.trips, d) == kind]
            if days:
                plugged = [self.connected[_day_steps(d, time_grid)].sum() * dt for d in days]
                hours[kind] = float(np.median(plugged))
        plugged = ", ".join(f"{h:g} h on {'weekdays' if k == 'weekday' else 'weekend days'}"
                            for k, h in hours.items())
        return (f"{len(self.departure_period)} departures, {miles:,.0f} miles, {kwh:,.0f} kWh driving "
                f"(at {kwh_per_mile:g} kWh/mile); plugged in {plugged}")


def _run_days(time_grid: TimeGrid) -> list[date]:
    start = time_grid.start
    end = start + timedelta(hours=time_grid.horizon_steps * time_grid.dt_hours)
    days, day = [], start.date()
    while datetime.combine(day, time()) < end:
        if datetime.combine(day, time()) >= start:
            days.append(day)
        day += timedelta(days=1)
    return days


def _day_steps(day: date, time_grid: TimeGrid) -> slice:
    step = time_grid.dt_hours * 3600
    first = int((datetime.combine(day, time()) - time_grid.start).total_seconds() // step)
    return slice(max(first, 0), min(first + round(86400 / step), time_grid.horizon_steps))


def _kind_of(trips: list[Trip], day: date) -> str | None:
    return next((t.kind for t in trips if t.day == day), None)


def _steps(moment: datetime, start: datetime, step_s: float, up: bool) -> int:
    offset = (moment - start).total_seconds() / step_s
    return math.ceil(offset - 1e-9) if up else math.floor(offset + 1e-9)


def _share_inside(trip: Trip, start: datetime, time_grid: TimeGrid) -> float:
    step_s = time_grid.dt_hours * 3600
    first, last = _steps(trip.depart, start, step_s, False), _steps(trip.arrive, start, step_s, True)
    inside = max(0, min(last, time_grid.horizon_steps) - max(first, 0))
    return inside / (last - first) if last > first else 0.0


def parse_pattern(name: str, raw: dict) -> EVPattern:
    try:
        return EVPattern.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'pattern'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"{name}: pattern: {problems}") from None


def expand_pattern(pattern: EVPattern, time_grid: TimeGrid, capacity_kwh: float, p_max_charge_kw: float,
                   eta_charge: float = 0.95, soc_min_frac: float = 0.0, soc_max_frac: float = 1.0,
                   name: str = "ev") -> ExpandedPattern:
    if time_grid.start is None:
        raise ValueError(f"{name}: an EV pattern needs time_grid.start")
    start, steps = time_grid.start, time_grid.horizon_steps
    step_s = time_grid.dt_hours * 3600
    end = start + timedelta(seconds=steps * step_s)
    usable = (soc_max_frac - soc_min_frac) * capacity_kwh

    trips: list[Trip] = []
    day = start.date() - timedelta(days=1)  # yesterday's trip may still be under way
    while datetime.combine(day, time()) < end:
        kind, spec = pattern.for_day(day)
        if spec != "home":
            depart_t, arrive_t = spec.times()
            depart = datetime.combine(day, depart_t)
            arrive = datetime.combine(day if arrive_t > depart_t else day + timedelta(days=1), arrive_t)
            kwh = spec.miles * pattern.kwh_per_mile
            if kwh > usable + 1e-9:
                raise ValueError(f"{name}: a {kind} trip of {spec.miles:g} miles takes {kwh:.1f} kWh, more than the "
                                 f"{usable:.1f} kWh the battery can give")
            trips.append(Trip(day, kind, depart, arrive, spec.miles, kwh))
        else:
            trips.append(Trip(day, kind, datetime.combine(day, time()), datetime.combine(day, time()), 0.0, 0.0))
        day += timedelta(days=1)
    real = [t for t in trips if t.arrive > t.depart]
    for a, b in pairwise(real):
        if b.depart < a.arrive:
            raise ValueError(f"{name}: the {a.kind} trip leaving {a.depart:%Y-%m-%d %H:%M} isn't back until "
                             f"{a.arrive:%H:%M}, after the next trip leaves ({b.depart:%Y-%m-%d %H:%M})")

    connected = np.ones(steps)
    driving = np.zeros(steps)
    for trip in real:
        first, last = _steps(trip.depart, start, step_s, False), _steps(trip.arrive, start, step_s, True)
        lo, hi = max(first, 0), min(last, steps)
        if hi <= lo:
            continue
        connected[lo:hi] = 0.0
        driving[lo:hi] += trip.kwh / (last - first)
    departures = [t for t in range(1, steps) if connected[t - 1] > 0.5 and connected[t] <= 0.5]

    notes: list[tuple[str, str]] = []
    minutes = round(step_s / 60)
    for kind in ("weekday", "weekend"):
        spec = getattr(pattern, kind)
        if spec == "home":
            continue
        rounded = []
        lost = 0
        for label, value, up in (("departure", spec.depart, False), ("arrival", spec.arrive, True)):
            t = time.fromisoformat(value)
            total = t.hour * 60 + t.minute
            if total % minutes:
                snapped = (math.ceil(total / minutes) if up else math.floor(total / minutes)) * minutes
                lost += abs(snapped - total)
                rounded.append(f"{value} {label} counts as {snapped // 60 % 24:02d}:{snapped % 60:02d}")
        if rounded:
            message = (f"{name} {kind}: {' and '.join(rounded)} on this {minutes}-minute run; {lost / 60:g} h less "
                       "plug-in per day; use 15- or 30-minute steps if the timing matters")
            notes.append(("info", message))

    # can the charger put back each trip's energy before the car leaves again?
    short = []
    for trip in real:
        back = _steps(trip.arrive, start, step_s, True)
        if back >= steps or trip.kwh <= 0:
            continue
        home = 0
        while back + home < steps and connected[back + home] > 0.5:
            home += 1
        if back + home >= steps:
            continue  # home until the run ends
        possible = p_max_charge_kw * home * time_grid.dt_hours * eta_charge
        if possible < trip.kwh - 1e-6:
            short.append((trip, home * time_grid.dt_hours, possible))
    if short:
        trip, hours, possible = short[0]
        message = (f"{name}: on {len(short)} day(s) (first {trip.day}) the car is home {hours:g} h after a "
                   f"{trip.kwh:.1f} kWh trip, and the charger can put back only {possible:.1f} kWh; it'll leave "
                   "short of its departure charge, or the run won't solve")
        notes.append(("warning", message))
    return ExpandedPattern(connected, driving, departures, trips, notes)


def pattern_device_params(name: str, params: dict, time_grid: TimeGrid) -> tuple[dict, ExpandedPattern]:
    """An ev_v2x device's params with `pattern` replaced by connected,
    departure_period and driving_kwh."""
    clash = [k for k in CLASHES if k in params]
    if clash:
        raise ValueError(f"{name}: pattern sets {', '.join(CLASHES)} itself; remove {', '.join(clash)} "
                         "or the pattern")
    params = dict(params)
    pattern = parse_pattern(name, params.pop("pattern"))
    expanded = expand_pattern(pattern, time_grid, params["capacity_kwh"], params["p_max_charge_kw"],
                              params.get("eta_charge", 0.95), params.get("soc_min_frac", 0.0),
                              params.get("soc_max_frac", 1.0), name)
    params |= {"connected": expanded.connected, "departure_period": expanded.departure_period,
               "driving_kwh": expanded.driving_kwh}
    return params, expanded


def scenario_patterns(devices, time_grid: TimeGrid) -> list[tuple[str, EVPattern, ExpandedPattern]]:
    """(device name, pattern, expansion) for every ev_v2x device config with a pattern."""
    found = []
    for device in devices:
        if device.type == "ev_v2x" and "pattern" in device.params:
            _, expanded = pattern_device_params(device.name, device.params, time_grid)
            found.append((device.name, parse_pattern(device.name, device.params["pattern"]), expanded))
    return found
