"""Settlement baselines (docs/DR_CAPACITY_SPEC.md v1.0, section 4, reference S).

  residential  5-in-10: of the 10 most recent eligible non-event days, the 5 with the
               highest load over the event hours, averaged
  commercial   10-in-10: the 10 most recent eligible non-event days, averaged
  day-of adjustment: x (event day's load / the baseline days' load) over the 4 hours
               ending 1 hour before the event, capped at +/-20%       [thread T1]
  weekend and holiday events: the same rule over weekend and holiday days, 4-in-4
               when fewer than 10 such days exist                   [thread T2]

"Load" is net load at the meter (import - export). Eligible days for a weekday event
are non-holiday weekdays; for a weekend or holiday event, weekends and holidays. Days
with any event are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from der_opt.dr.calendar import Event, day_type

LOOKBACK_DAYS = 10
ADJUSTMENT_HOURS = 4  # T1
ADJUSTMENT_GAP_HOURS = 1  # T1
ADJUSTMENT_CAP = 0.20  # T1
WEEKEND_FALLBACK = 4  # T2


@dataclass
class Baseline:
    event_id: str
    values: np.ndarray  # kW per event step (NaN if no baseline days)
    days_used: list[date]
    adjustment: float
    note: str = ""


def _same_hours(net: pd.Series, day: date, event: Event, offset_start: pd.Timedelta, offset_end: pd.Timedelta) -> pd.Series:
    """Load on `day` over the event's clock hours shifted by offsets."""
    shift = pd.Timestamp(day) - pd.Timestamp(event.day)
    return net.loc[event.start + shift + offset_start: event.end + shift + offset_end - pd.Timedelta(microseconds=1)]


def settlement_baselines(net: pd.Series, events: list[Event], customer_class: str) -> dict[str, Baseline]:
    """S for every event. `net` is net load (kW) on the local clock, one value per step."""
    event_days = {e.day for e in events}
    all_days = sorted(set(net.index.date))
    out = {}
    for event in events:
        weekday_event = day_type(event.day) == "weekday"
        pool = [d for d in all_days if d < event.day and d not in event_days
                and (day_type(d) == "weekday") == weekday_event]
        recent = pool[-LOOKBACK_DAYS:]
        note = ""
        if not weekday_event and len(recent) < LOOKBACK_DAYS:
            recent = pool[-WEEKEND_FALLBACK:]
            chosen = recent
            note = f"weekend/holiday fallback {len(chosen)}-in-{WEEKEND_FALLBACK}"
        elif customer_class == "residential":
            loads = {d: float(_same_hours(net, d, event, pd.Timedelta(0), pd.Timedelta(0)).sum()) for d in recent}
            chosen = sorted(sorted(recent, key=lambda d: -loads[d])[:5])
            note = f"5-in-{len(recent)}" if len(recent) < LOOKBACK_DAYS else ""
        else:
            chosen = recent
            note = f"10-in-{len(recent)}" if len(recent) < LOOKBACK_DAYS else ""
        steps = len(net.loc[event.start: event.end - pd.Timedelta(microseconds=1)])
        if not chosen:
            out[event.event_id] = Baseline(event.event_id, np.full(steps, np.nan), [], 1.0, "no eligible baseline days")
            continue
        profile = np.mean([_same_hours(net, d, event, pd.Timedelta(0), pd.Timedelta(0)).to_numpy() for d in chosen], axis=0)

        start = -pd.Timedelta(hours=ADJUSTMENT_HOURS + ADJUSTMENT_GAP_HOURS)
        end_offset = -pd.Timedelta(hours=ADJUSTMENT_GAP_HOURS) - (event.end - event.start)
        today = _same_hours(net, event.day, event, start, end_offset)
        before = np.mean([_same_hours(net, d, event, start, end_offset).to_numpy() for d in chosen], axis=0)
        adjustment = 1.0
        if len(today) and len(today) == len(before) and before.sum() > 0 and today.sum() > 0:
            adjustment = float(np.clip(today.sum() / before.sum(), 1 - ADJUSTMENT_CAP, 1 + ADJUSTMENT_CAP))
        out[event.event_id] = Baseline(event.event_id, profile * adjustment, chosen, adjustment, note)
    return out


def baseline_series(baselines: dict[str, Baseline], events: list[Event], index: pd.DatetimeIndex) -> pd.Series:
    """S as a series over the run, NaN outside events."""
    s = pd.Series(np.nan, index=index)
    for event in events:
        rows = s.loc[event.start: event.end - pd.Timedelta(microseconds=1)].index
        s.loc[rows] = baselines[event.event_id].values
    return s
