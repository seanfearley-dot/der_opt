"""PG&E's time-of-use holidays: New Year's Day, Presidents' Day, Memorial Day,
Independence Day, Labor Day, Veterans Day, Thanksgiving Day, and Christmas Day, "on
which the holidays are legally observed" (PG&E Schedule E-19, HOLIDAYS). Fixed-date
holidays falling on a Saturday are observed the Friday before, on a Sunday the Monday
after (the federal observance rule).

The NBT export compensation rate distinguishes weekdays from weekends/holidays; the NBT
schedule doesn't list its holidays, so this assumes the same list the TOU schedules use.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@cache
def pge_holidays(year: int) -> frozenset[date]:
    monday, thursday = 0, 3
    return frozenset({
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 2, monday, 3),
        _last_weekday(year, 5, monday),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, monday, 1),
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, thursday, 4),
        _observed(date(year, 12, 25)),
    })


def is_weekend_or_holiday(day: date) -> bool:
    # A Saturday New Year's Day is observed on Dec 31 of the *prior* year, so check the
    # following year's set too.
    return day.weekday() >= 5 or day in pge_holidays(day.year) or day in pge_holidays(day.year + 1)
