from datetime import datetime
from pathlib import Path

import pytest

from der_opt.scenarios.data_sources import resolve_array_param
from der_opt.time_grid import TimeGrid

LBNL_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "lbnl_sample"
EV_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ev_model_sample"


def test_resolve_array_param_passes_through_a_literal_list():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    assert resolve_array_param([1.0, 2.0, 3.0], tg) == [1.0, 2.0, 3.0]


def test_resolve_array_param_passes_through_non_source_values():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3)
    assert resolve_array_param(0.5, tg) == 0.5
    assert resolve_array_param({"not_a_source_key": 1}, tg) == {"not_a_source_key": 1}


def test_lbnl_net_load_source():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2026, 1, 1))
    result = resolve_array_param(
        {"source": "lbnl_net_load", "scenario_dir": str(LBNL_FIXTURE_DIR),
         "cluster_name": "res-test-sin_fam-small"},
        tg,
    )
    assert result.tolist() == pytest.approx([10.0, 10.0, 12.0])


def test_lbnl_gross_load_source():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4, start=datetime(2026, 1, 1))
    result = resolve_array_param(
        {"source": "lbnl_gross_load", "scenario_dir": str(LBNL_FIXTURE_DIR),
         "cluster_name": "res-test-sin_fam-small", "start_hour_ending": 1},
        tg,
    )
    assert result.tolist() == pytest.approx([10.0, 10.0, 12.0, 12.0])


def test_lbnl_pv_source():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=6, start=datetime(2026, 1, 1))
    result = resolve_array_param(
        {"source": "lbnl_pv", "scenario_dir": str(LBNL_FIXTURE_DIR),
         "cluster_name": "res-test-sin_fam-small"},
        tg,
    )
    assert result.tolist() == pytest.approx([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])


def test_ev_connected_schedule_source():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 6, 1))
    result = resolve_array_param(
        {
            "source": "ev_connected_schedule",
            "arrivals_csv": str(EV_FIXTURE_DIR / "raw_arrivals.csv"),
            "departures_csv": str(EV_FIXTURE_DIR / "raw_departures.csv"),
        },
        tg,
    )
    assert len(result) == 24
    assert set(result.tolist()) <= {0.0, 1.0}


def test_unknown_source_raises_with_choices():
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1)
    with pytest.raises(ValueError, match="lbnl_net_load"):
        resolve_array_param({"source": "not_a_real_source"}, tg)
