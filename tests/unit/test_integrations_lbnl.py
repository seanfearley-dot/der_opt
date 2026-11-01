from datetime import datetime
from pathlib import Path

import pytest

from der_opt.integrations.lbnl_load import (
    list_clusters,
    premise_gross_load_kw,
    premise_net_load_kw,
    premise_solar_available_kw,
)
from der_opt.time_grid import TimeGrid

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "lbnl_sample"


def test_list_clusters_filters_by_sector():
    clusters = list_clusters(FIXTURE_DIR, sector="res")
    assert clusters == ["res-test-sin_fam-small", "res-test-mult_fam-small"]


def test_premise_net_load_kw_matches_hand_calc():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=6, start=datetime(2026, 1, 1))
    load = premise_net_load_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg, start_hour_ending=1)
    assert load == pytest.approx([10.0, 10.0, 12.0, 7.0, 3.0, 3.0])


def test_premise_gross_load_kw_does_not_subtract_pv():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=6, start=datetime(2026, 1, 1))
    load = premise_gross_load_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg, start_hour_ending=1)
    assert load == pytest.approx([10.0, 10.0, 12.0, 12.0, 8.0, 8.0])


def test_premise_solar_available_kw_matches_hand_calc():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=6, start=datetime(2026, 1, 1))
    solar = premise_solar_available_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg, start_hour_ending=1)
    assert solar == pytest.approx([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])


def test_start_hour_ending_slices_a_sub_window():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 1, 1))
    load = premise_net_load_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg, start_hour_ending=8)
    assert load == pytest.approx([9.0, 11.0, 11.0])


def test_requesting_more_hours_than_available_raises():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=20, start=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        premise_net_load_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg)


def test_non_hourly_time_grid_rejected():
    tg = TimeGrid(resolution_minutes=15, horizon_steps=4, start=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        premise_net_load_kw(FIXTURE_DIR, "res-test-sin_fam-small", tg)


def test_unknown_cluster_name_raises():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        premise_net_load_kw(FIXTURE_DIR, "does-not-exist", tg)
