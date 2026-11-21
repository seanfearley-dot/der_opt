"""DR event calendars (docs/DR_CAPACITY_SPEC.md v1.0, sections 1-2): the four
constructors -- events file, price-based, randomized, fixed schedule -- and the
program-rule validation every calendar must pass.

Times are on the run's local clock (naive timestamps), the clock the time grid uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.billing.holidays import pge_holidays
from der_opt.diagnostics import Finding
from der_opt.time_grid import TimeGrid

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Event:
    event_id: str
    program_id: str
    event_type: str  # dispatch | test
    start: pd.Timestamp  # local, naive
    end: pd.Timestamp  # exclusive
    scope: str = "system"
    source: str = ""  # which constructor made it

    @property
    def hours(self) -> float:
        return (self.end - self.start) / pd.Timedelta(hours=1)

    @property
    def day(self) -> date:
        return self.start.date()


class CalendarError(ValueError):
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("DR event calendar breaks its program's rules:\n" + "\n".join(f"  - {f.message}" for f in findings))


def _window(program) -> tuple[time, time]:
    first, last = program.window.split("-")
    start = datetime.strptime(first.strip(), "%H:%M").time()
    end_text = last.strip()
    end = time(23, 59, 59, 999999) if end_text in ("24:00", "00:00") else datetime.strptime(end_text, "%H:%M").time()
    return start, end


def day_type(day: date) -> str:
    if day in pge_holidays(day.year):
        return "holiday"
    return "weekend" if day.weekday() >= 5 else "weekday"


def eligible_days(program, time_grid: TimeGrid) -> list[date]:
    """Days in the run where the program could call an event: in season, of an allowed
    day type, with the whole window inside the run."""
    stamps = pd.DatetimeIndex(time_grid.timestamps())
    run_end = stamps[-1] + pd.Timedelta(minutes=time_grid.resolution_minutes)
    window_start, window_end = _window(program)
    days = []
    for day in sorted(set(stamps.date)):
        if day.month not in program.season or day_type(day) not in program.day_types:
            continue
        opens = pd.Timestamp(datetime.combine(day, window_start))
        closes = _window_close(day, window_end)
        if opens >= stamps[0] and closes <= run_end:
            days.append(day)
    return days


def _window_close(day: date, window_end: time) -> pd.Timestamp:
    if window_end == time(23, 59, 59, 999999):
        return pd.Timestamp(datetime.combine(day + timedelta(days=1), time(0)))
    return pd.Timestamp(datetime.combine(day, window_end))


def _steps_in_window(day: date, program, time_grid: TimeGrid) -> pd.DatetimeIndex:
    window_start, window_end = _window(program)
    return pd.date_range(pd.Timestamp(datetime.combine(day, window_start)), _window_close(day, window_end),
                         freq=f"{time_grid.resolution_minutes}min", inclusive="left")


def _event(program, day: date, start: pd.Timestamp, hours: float, event_type: str, source: str, n: int = 1) -> Event:
    return Event(f"{program.program_id}-{day:%Y%m%d}-{n}", program.program_id, event_type, start,
                 start + pd.Timedelta(hours=hours), "system", source)


# --- validation ---------------------------------------------------------------------


def validate_calendar(events: list[Event], program, time_grid: TimeGrid) -> list[Finding]:
    """Every rule in spec sections 1-2 the calendar breaks, as error findings."""
    found = []

    def bad(message):
        found.append(Finding("error", "dr calendar", message))

    stamps = pd.DatetimeIndex(time_grid.timestamps())
    step = pd.Timedelta(minutes=time_grid.resolution_minutes)
    run_end = stamps[-1] + step
    window_start, window_end = _window(program)
    for e in events:
        if e.program_id != program.program_id:
            bad(f"{e.event_id}: program {e.program_id!r}, but the scenario's program is {program.program_id!r}")
        if e.event_type not in ("dispatch", "test"):
            bad(f"{e.event_id}: event_type must be dispatch or test")
        if e.end <= e.start:
            bad(f"{e.event_id}: ends before it starts")
            continue
        if e.start < stamps[0] or e.end > run_end:
            bad(f"{e.event_id}: outside the run ({stamps[0]} to {run_end})")
        if (e.start - stamps[0]) % step or (e.end - stamps[0]) % step:
            bad(f"{e.event_id}: doesn't start and end on the run's {time_grid.resolution_minutes}-minute steps")
        if e.day.month not in program.season:
            bad(f"{e.event_id}: month {e.day.month} is outside the program's season")
        if day_type(e.day) not in program.day_types:
            bad(f"{e.event_id}: a {day_type(e.day)}, not one of {program.day_types}")
        if e.start < pd.Timestamp(datetime.combine(e.day, window_start)) or e.end > _window_close(e.day, window_end):
            bad(f"{e.event_id}: {e.start.time()}-{e.end.time()} isn't inside the {program.window} window")
        if not program.min_hours - 1e-9 <= e.hours <= program.max_hours + 1e-9:
            bad(f"{e.event_id}: {e.hours:g} h, outside {program.min_hours:g}-{program.max_hours:g} h")

    ordered = sorted(events, key=lambda e: e.start)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.start < a.end:
            bad(f"{a.event_id} and {b.event_id} overlap")
    by_month = pd.Series([e.day.month for e in events]).value_counts()
    if program.max_events_per_month is not None:
        for month, count in by_month.items():
            if count > program.max_events_per_month:
                bad(f"{count} events in month {month}, over the limit of {program.max_events_per_month}")
    if program.max_events_per_year is not None and len(events) > program.max_events_per_year:
        bad(f"{len(events)} events, over the yearly limit of {program.max_events_per_year}")
    if program.max_consecutive_days is not None:
        days = sorted({e.day for e in events})
        run = 1
        for a, b in zip(days, days[1:], strict=False):
            run = run + 1 if (b - a).days == 1 else 1
            if run > program.max_consecutive_days:
                bad(f"events on {run} consecutive days ending {b}, over the limit of {program.max_consecutive_days}")
                break
    return found


def check_calendar(events: list[Event], program, time_grid: TimeGrid) -> list[Event]:
    found = validate_calendar(events, program, time_grid)
    if found:
        raise CalendarError(found)
    return sorted(events, key=lambda e: e.start)


# --- constructors -------------------------------------------------------------------


def from_file(path: str | Path, program, time_grid: TimeGrid, tz: str) -> list[Event]:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in ("event_id", "start", "end", "program_id", "event_type") if c not in frame]
    if missing:
        raise ValueError(f"{path}: missing column(s) {missing}")
    events = []
    for _, row in frame.iterrows():
        start, end = (pd.Timestamp(row[c]) for c in ("start", "end"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError(f"{path}: {row['event_id']}: times need a UTC offset (ISO 8601)")
        events.append(Event(row["event_id"].strip(), row["program_id"].strip(), row["event_type"].strip(),
                            start.tz_convert(tz).tz_localize(None), end.tz_convert(tz).tz_localize(None),
                            (row.get("scope") or "system").strip(), "file"))
    return check_calendar(events, program, time_grid)


def _limits_allow(chosen: list[date], day: date, program, per_month: dict[int, int] | None, min_gap: int = 0) -> bool:
    month_count = sum(1 for d in chosen if d.month == day.month)
    if program.max_events_per_month is not None and month_count >= program.max_events_per_month:
        return False
    if per_month is not None and month_count >= per_month.get(day.month, 0):
        return False
    if program.max_events_per_year is not None and len(chosen) >= program.max_events_per_year:
        return False
    if any(abs((d - day).days) <= min_gap for d in chosen):
        return False
    if program.max_consecutive_days is not None:
        days = set(chosen) | {day}
        run, d = 1, day
        while d - timedelta(days=1) in days:
            run, d = run + 1, d - timedelta(days=1)
        d = day
        while d + timedelta(days=1) in days:
            run, d = run + 1, d + timedelta(days=1)
        if run > program.max_consecutive_days:
            return False
    return True


def price_based(price: np.ndarray, program, time_grid: TimeGrid, settings) -> list[Event]:
    """The highest-price days, each event sized fixed or by how long price stays high."""
    stamps = pd.DatetimeIndex(time_grid.timestamps())
    value = pd.Series(np.asarray(price, dtype=float), index=stamps)
    dt = time_grid.dt_hours
    days = eligible_days(program, time_grid)
    scores = {d: float(value.loc[_steps_in_window(d, program, time_grid)].mean()) for d in days}
    per_month = None
    if settings.events_per_month is not None:
        per_month = {m: settings.events_per_month for m in range(1, 13)}
    target = settings.events_per_year
    chosen: list[date] = []
    for day in sorted(days, key=lambda d: -scores[d]):
        if target is not None and len(chosen) >= target:
            break
        if _limits_allow(chosen, day, program, per_month):
            chosen.append(day)

    events = []
    for day in sorted(chosen):
        window = value.loc[_steps_in_window(day, program, time_grid)]
        if settings.duration == "fixed":
            hours = settings.hours or program.max_hours
            n = round(hours / dt)
            sums = window.rolling(n).sum().shift(-(n - 1)).dropna()
            start = sums.idxmax()  # the highest-value block of the fixed length
        else:
            n_min, n_max = round(program.min_hours / dt), round(program.max_hours / dt)
            peak = int(np.argmax(window.to_numpy()))
            lo = hi = peak
            threshold = settings.alpha * window.iloc[peak]
            while hi - lo + 1 < n_max:
                left = window.iloc[lo - 1] if lo > 0 else -np.inf
                right = window.iloc[hi + 1] if hi + 1 < len(window) else -np.inf
                best = max(left, right)
                if best == -np.inf or (best < threshold and hi - lo + 1 >= n_min):
                    break
                if right >= left:
                    hi += 1
                else:
                    lo -= 1
            start, hours = window.index[lo], (hi - lo + 1) * dt
        events.append(_event(program, day, start, hours, settings.event_type, f"price/{settings.duration}"))
    return check_calendar(events, program, time_grid)


def loading_triggered(loading_pct: np.ndarray, program, time_grid: TimeGrid, above_pct: float,
                      duration: str = "variable", hours: float | None = None) -> list[Event]:
    """Feeder DR (docs/COORDINATION_SPEC.md section 5): events on the days an element's
    forecast loading goes above `above_pct` of its rating inside the program window,
    highest first, within the program's limits. variable: the steps over the trigger
    around the day's peak, held to min_hours..max_hours; fixed: `hours` (default
    max_hours) at the day's highest-loading block."""
    stamps = pd.DatetimeIndex(time_grid.timestamps())
    value = pd.Series(np.asarray(loading_pct, dtype=float), index=stamps)
    dt = time_grid.dt_hours
    peaks = {}
    for day in eligible_days(program, time_grid):
        window = value.loc[_steps_in_window(day, program, time_grid)]
        if len(window) and window.max() > above_pct:
            peaks[day] = float(window.max())
    chosen: list[date] = []
    for day in sorted(peaks, key=lambda d: -peaks[d]):
        if _limits_allow(chosen, day, program, None):
            chosen.append(day)
    events = []
    n_min, n_max = round(program.min_hours / dt), round(program.max_hours / dt)
    for day in sorted(chosen):
        window = value.loc[_steps_in_window(day, program, time_grid)]
        if duration == "fixed":
            n = min(round((hours or program.max_hours) / dt), len(window))
            sums = window.rolling(n).sum().shift(-(n - 1)).dropna()
            start, length = sums.idxmax(), n
        else:
            over = window.to_numpy() > above_pct
            peak = int(np.argmax(window.to_numpy()))
            lo = hi = peak
            while lo > 0 and over[lo - 1] and hi - lo + 1 < n_max:
                lo -= 1
            while hi + 1 < len(window) and over[hi + 1] and hi - lo + 1 < n_max:
                hi += 1
            while hi - lo + 1 < n_min:  # too short: widen toward the higher neighbor
                left = window.iloc[lo - 1] if lo > 0 else -np.inf
                right = window.iloc[hi + 1] if hi + 1 < len(window) else -np.inf
                if left == right == -np.inf:
                    break
                if right >= left:
                    hi += 1
                else:
                    lo -= 1
            start, length = window.index[lo], hi - lo + 1
        events.append(_event(program, day, start, length * dt, "dispatch", f"loading>{above_pct:g}%/{duration}"))
    return check_calendar(events, program, time_grid)


def randomized(program, time_grid: TimeGrid, settings, replicate: int = 0, price: np.ndarray | None = None) -> list[Event]:
    """The spec's 1c proposal: a set number of events per month on randomly drawn
    eligible days (uniform, or weighted toward high prices), at random allowed starts,
    reproducible from seed + replicate."""
    rng = np.random.default_rng(settings.seed + replicate)
    dt = time_grid.dt_hours
    stamps = pd.DatetimeIndex(time_grid.timestamps())
    value = pd.Series(np.asarray(price, dtype=float), index=stamps) if price is not None else None
    per_month = settings.events_per_month
    per_month = per_month if isinstance(per_month, dict) else {m: per_month for m in range(1, 13)}
    days = eligible_days(program, time_grid)
    chosen: list[date] = []
    for month in sorted({d.month for d in days}):
        pool = [d for d in days if d.month == month]
        wanted = per_month.get(month, 0)
        while pool and sum(1 for d in chosen if d.month == month) < wanted:
            if settings.weighting == "price":
                if value is None:
                    raise ValueError("weighting: price needs a price series (random.series)")
                weights = np.array([max(float(value.loc[_steps_in_window(d, program, time_grid)].mean()), 0.0) for d in pool])
                weights = weights ** settings.weight_power
                probabilities = weights / weights.sum() if weights.sum() > 0 else None
            else:
                probabilities = None
            day = pool[rng.choice(len(pool), p=probabilities)]
            pool.remove(day)
            if _limits_allow(chosen, day, program, None, settings.min_days_between):
                chosen.append(day)

    events = []
    for day in sorted(chosen):
        if settings.hours is not None:
            hours = settings.hours
        else:
            options = np.arange(program.min_hours, program.max_hours + dt / 2, dt)
            hours = float(rng.choice(options))
        window = _steps_in_window(day, program, time_grid)
        n = round(hours / dt)
        starts = window[: len(window) - n + 1]
        events.append(_event(program, day, starts[rng.integers(len(starts))], hours, settings.event_type,
                             f"random/seed {settings.seed + replicate}"))
    return check_calendar(events, program, time_grid)


def schedule(rules, program, time_grid: TimeGrid) -> list[Event]:
    """Recurring rules expanded into events, in order."""
    stamps = pd.DatetimeIndex(time_grid.timestamps())
    days_in_run = sorted(set(stamps.date))
    taken: dict[date, str] = {}
    events, problems = [], []
    for number, rule in enumerate(rules, 1):
        if rule.dates is not None:
            candidates = [date.fromisoformat(d) for d in rule.dates]
        else:
            months = range(1, 13) if rule.months == "all" else rule.months
            candidates = [d for d in days_in_run if d.month in months]
            if rule.nth_weekday is not None:
                weekday, n = WEEKDAYS[str(rule.nth_weekday["weekday"]).lower()[:3]], int(rule.nth_weekday["n"])
                candidates = [d for d in candidates if d.weekday() == weekday and (d.day - 1) // 7 + 1 == n]
            elif rule.days == "weekdays":
                candidates = [d for d in candidates if d.weekday() < 5]
            elif rule.days == "weekends":
                candidates = [d for d in candidates if d.weekday() >= 5]
            elif isinstance(rule.days, list):
                wanted = {WEEKDAYS[str(x).lower()[:3]] for x in rule.days}
                candidates = [d for d in candidates if d.weekday() in wanted]
        start_time = datetime.strptime(rule.start, "%H:%M").time()
        for day in candidates:
            if day_type(day) == "holiday" and "holiday" not in program.day_types:
                continue
            if day in taken:
                problems.append(Finding("error", "dr calendar", f"schedule rule {number} lands on {day}, which rule "
                                        f"{taken[day]} already uses"))
                continue
            taken[day] = str(number)
            events.append(_event(program, day, pd.Timestamp(datetime.combine(day, start_time)), rule.hours,
                                 rule.event_type, f"schedule/rule {number}"))
    if problems:
        raise CalendarError(problems)
    return check_calendar(events, program, time_grid)


def calendar_frame(events: list[Event]) -> pd.DataFrame:
    return pd.DataFrame([{
        "event_id": e.event_id, "program_id": e.program_id, "event_type": e.event_type,
        "start": e.start, "end": e.end, "hours": e.hours, "scope": e.scope, "source": e.source,
    } for e in events], columns=["event_id", "program_id", "event_type", "start", "end", "hours", "scope", "source"])
