from datetime import datetime

import pytest

from der_opt.time_grid import TimeGrid


def test_dt_hours_from_resolution():
    grid = TimeGrid(resolution_minutes=15, horizon_steps=4)
    assert grid.dt_hours == pytest.approx(0.25)


def test_periods_is_zero_indexed_range():
    grid = TimeGrid(resolution_minutes=60, horizon_steps=3)
    assert list(grid.periods) == [0, 1, 2]


def test_timestamps_step_by_resolution():
    start = datetime(2026, 1, 1, 0, 0)
    grid = TimeGrid(resolution_minutes=30, horizon_steps=3, start=start)
    assert grid.timestamps() == [
        datetime(2026, 1, 1, 0, 0),
        datetime(2026, 1, 1, 0, 30),
        datetime(2026, 1, 1, 1, 0),
    ]


def test_timestamps_without_start_raises():
    grid = TimeGrid(resolution_minutes=60, horizon_steps=2)
    with pytest.raises(ValueError):
        grid.timestamps()


def test_invalid_resolution_or_horizon_rejected():
    with pytest.raises(ValueError):
        TimeGrid(resolution_minutes=0, horizon_steps=4)
    with pytest.raises(ValueError):
        TimeGrid(resolution_minutes=15, horizon_steps=0)
