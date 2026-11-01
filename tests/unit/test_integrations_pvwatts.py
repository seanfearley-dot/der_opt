from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from der_opt.integrations.pvwatts import (
    DEMO_KEY,
    fetch_pvwatts_hourly_ac_kw,
    solar_availability_kw,
)
from der_opt.time_grid import TimeGrid


def _mock_response(ac_watts, errors=None):
    mock = MagicMock()
    mock.json.return_value = {"outputs": {"ac": ac_watts}, "errors": errors or []}
    mock.raise_for_status.return_value = None
    return mock


def test_fetch_converts_watts_to_kw_and_uses_demo_key_by_default():
    ac_watts = [1000.0] * 8760
    with patch("der_opt.integrations.pvwatts.requests.get", return_value=_mock_response(ac_watts)) as mock_get:
        result = fetch_pvwatts_hourly_ac_kw(lat=37.8, lon=-122.3, system_capacity_kw=5.0)

    assert result == pytest.approx(np.full(8760, 1.0))
    called_params = mock_get.call_args.kwargs["params"]
    assert called_params["api_key"] == DEMO_KEY
    assert called_params["tilt"] == pytest.approx(37.8)  # defaults to latitude


def test_fetch_uses_env_var_api_key(monkeypatch):
    monkeypatch.setenv("NREL_API_KEY", "my-real-key")
    with patch("der_opt.integrations.pvwatts.requests.get", return_value=_mock_response([0.0] * 8760)) as mock_get:
        fetch_pvwatts_hourly_ac_kw(lat=37.8, lon=-122.3, system_capacity_kw=5.0)
    assert mock_get.call_args.kwargs["params"]["api_key"] == "my-real-key"


def test_fetch_raises_on_api_error_payload():
    with patch(
        "der_opt.integrations.pvwatts.requests.get",
        return_value=_mock_response([], errors=["Invalid api_key"]),
    ), pytest.raises(RuntimeError):
        fetch_pvwatts_hourly_ac_kw(lat=37.8, lon=-122.3, system_capacity_kw=5.0)


def test_solar_availability_kw_slices_the_requested_window():
    hourly = np.arange(8760, dtype=float)
    tg = TimeGrid(resolution_minutes=60, horizon_steps=5)
    sliced = solar_availability_kw(tg, hourly, start_hour=100)
    assert sliced.tolist() == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_solar_availability_kw_rejects_non_hourly_grid():
    tg = TimeGrid(resolution_minutes=15, horizon_steps=4)
    with pytest.raises(ValueError):
        solar_availability_kw(tg, np.zeros(8760))
