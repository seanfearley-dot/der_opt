from datetime import datetime

import pytest

from der_opt.pricing import DailyTOUPriceSeries
from der_opt.time_grid import TimeGrid


def test_daily_tou_repeats_pattern_across_multiple_days():
    pattern = list(range(24))  # distinct value per hour, easy to check
    series = DailyTOUPriceSeries(pattern)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=48, start=datetime(2026, 1, 1))
    values = series.values(tg)
    assert values.tolist() == pattern + pattern


def test_daily_tou_requires_exactly_24_values():
    with pytest.raises(ValueError):
        DailyTOUPriceSeries([0.1] * 23)


def test_daily_tou_requires_time_grid_start():
    series = DailyTOUPriceSeries([0.1] * 24)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24)
    with pytest.raises(ValueError):
        series.values(tg)


def test_daily_tou_requires_hourly_resolution():
    series = DailyTOUPriceSeries([0.1] * 24)
    tg = TimeGrid(resolution_minutes=15, horizon_steps=96, start=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        series.values(tg)
