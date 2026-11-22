from datetime import datetime

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("netCDF4")

from der_opt.integrations.load_forecast import (
    describe_forecast,
    forecast_profile_kw,
    forecast_traces_kw,
)
from der_opt.scenarios.data_sources import resolve_array_param
from der_opt.time_grid import TimeGrid

TZ = "America/Los_Angeles"


def _write(path, *, entities=("BANK",), types=("bank",), n_traces=5, year=2025, n_customers=None,
           attrs=None):
    """A small file shaped like loadcast's output contract: net[k, t, e] = 100*(k+1) + hour_utc
    + 1000*e, pv = 10, gross = net + pv; quantiles at 0.1/0.5/0.9."""
    time = pd.date_range(f"{year}-01-01 08:00", f"{year + 1}-01-01 07:00", freq="h")  # local year in UTC
    hours = time.hour.to_numpy()
    k = np.arange(n_traces)[:, None, None]
    e = np.arange(len(entities))[None, None, :]
    net = (100.0 * (k + 1) + hours[None, :, None] + 1000.0 * e).astype(np.float32)
    pv = np.full_like(net, 10.0)
    q = np.array([0.1, 0.5, 0.9])
    coords = {"time": time, "entity": list(entities), "entity_type": ("entity", list(types)),
              "trace": np.arange(n_traces), "quantile": q}
    if n_customers is not None:
        coords["n_customers"] = ("entity", np.asarray(n_customers, dtype=float))
    ds = xr.Dataset(
        {"net": (("trace", "time", "entity"), net), "pv": (("trace", "time", "entity"), pv),
         "gross": (("trace", "time", "entity"), net + pv),
         "net_q": (("quantile", "time", "entity"), np.quantile(net, q, axis=0))},
        coords=coords,
        attrs={"contract_version": "1", "kind": "planning", "units": "kW", "scenario": "test", "tz": TZ,
               **(attrs or {})},
    )
    ds.to_netcdf(path, engine="netcdf4")
    return path


@pytest.fixture
def bank_file(tmp_path):
    return _write(tmp_path / "bank_traces.nc")


def test_local_grid_reads_the_matching_utc_hours(bank_file):
    # July: Pacific daylight time, UTC-7 -> local midnight is 07:00 UTC
    tg = TimeGrid(resolution_minutes=60, horizon_steps=3, start=datetime(2025, 7, 1, 0))
    out = forecast_profile_kw(bank_file, tg, trace=0)
    assert out.tolist() == pytest.approx([107.0, 108.0, 109.0])
    # January: standard time, UTC-8
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2025, 1, 10, 0))
    assert forecast_profile_kw(bank_file, tg, trace=1).tolist() == pytest.approx([208.0, 209.0])


def test_sub_hourly_grid_holds_the_hourly_value(bank_file):
    tg = TimeGrid(resolution_minutes=15, horizon_steps=8, start=datetime(2025, 7, 1, 0))
    out = forecast_profile_kw(bank_file, tg, trace=0)
    assert out.tolist() == pytest.approx([107.0] * 4 + [108.0] * 4)


def test_named_traces_follow_the_annual_peak(bank_file):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2025, 7, 1, 0))
    # peaks rise with the trace index (100*(k+1) + 23), so max = last, poe50 = middle
    assert forecast_profile_kw(bank_file, tg, trace="max")[0] == pytest.approx(507.0)
    assert forecast_profile_kw(bank_file, tg, trace="poe50")[0] == pytest.approx(307.0)
    assert forecast_profile_kw(bank_file, tg, trace="poe10")[0] == pytest.approx(507.0)
    assert forecast_profile_kw(bank_file, tg, trace="poe90")[0] == pytest.approx(107.0)
    with pytest.raises(ValueError, match="poe10"):
        forecast_profile_kw(bank_file, tg, trace="worst")


def test_variables_quantiles_and_scale(bank_file):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2025, 7, 1, 0))
    assert forecast_profile_kw(bank_file, tg, trace=0, var="gross")[0] == pytest.approx(117.0)
    assert forecast_profile_kw(bank_file, tg, trace=0, var="pv")[0] == pytest.approx(10.0)
    assert forecast_profile_kw(bank_file, tg, quantile=0.5)[0] == pytest.approx(307.0)
    assert forecast_profile_kw(bank_file, tg, trace=0, scale=0.5)[0] == pytest.approx(53.5)
    with pytest.raises(ValueError, match="not in the file"):
        forecast_profile_kw(bank_file, tg, quantile=0.75)
    with pytest.raises(ValueError, match="exactly one"):
        forecast_profile_kw(bank_file, tg, trace=0, quantile=0.5)
    with pytest.raises(ValueError, match="exactly one"):
        forecast_profile_kw(bank_file, tg)


def test_per_customer_class_profile(tmp_path):
    path = _write(tmp_path / "classes.nc", entities=("res_nopv", "new_office"), types=("class", "customer"),
                  n_customers=[400.0, 1.0])
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2025, 7, 1, 0))
    total = forecast_profile_kw(path, tg, entity="res_nopv", trace=0)[0]
    each = forecast_profile_kw(path, tg, entity="res_nopv", trace=0, per_customer=True)[0]
    assert each == pytest.approx(total / 400)
    assert forecast_profile_kw(path, tg, entity="new_office", trace=0)[0] == pytest.approx(1107.0)
    with pytest.raises(ValueError, match="several entities"):
        forecast_profile_kw(path, tg, trace=0)
    with pytest.raises(ValueError, match="not in the forecast"):
        forecast_profile_kw(path, tg, entity="res_pv", trace=0)
    bank = _write(tmp_path / "bank.nc")
    with pytest.raises(ValueError, match="n_customers"):
        forecast_profile_kw(bank, tg, trace=0, per_customer=True)


def test_grid_outside_the_forecast_and_align_year(bank_file):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2026, 7, 1, 0))
    with pytest.raises(ValueError, match="align_year"):
        forecast_profile_kw(bank_file, tg, trace=0)
    assert forecast_profile_kw(bank_file, tg, trace=0, align_year=True).tolist() == pytest.approx([107.0, 108.0])
    leap = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2028, 2, 29, 12))
    # Feb 29 12:00 PST -> Feb 28 2025 20:00 UTC
    assert forecast_profile_kw(bank_file, leap, trace=0, align_year=True)[0] == pytest.approx(120.0)


def test_contract_version_and_tz_are_checked(tmp_path):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=1, start=datetime(2025, 7, 1, 0))
    bad = _write(tmp_path / "v2.nc", attrs={"contract_version": "2"})
    with pytest.raises(ValueError, match="contract_version"):
        forecast_profile_kw(bad, tg, trace=0)
    no_tz = tmp_path / "no_tz.nc"
    ds = xr.open_dataset(_write(tmp_path / "src.nc")).load()
    del ds.attrs["tz"]
    ds.to_netcdf(no_tz)
    with pytest.raises(ValueError, match="tz"):
        forecast_profile_kw(no_tz, tg, trace=0)
    assert forecast_profile_kw(no_tz, tg, trace=0, tz=TZ)[0] == pytest.approx(107.0)


def test_traces_and_describe(bank_file):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=4, start=datetime(2025, 7, 1, 0))
    all_traces = forecast_traces_kw(bank_file, tg)
    assert all_traces.shape == (5, 4) and all_traces[:, 0].tolist() == pytest.approx([107, 207, 307, 407, 507])
    info = describe_forecast(bank_file)
    assert info["n_traces"] == 5 and info["entities"]["BANK"]["type"] == "bank"
    assert info["quantiles"] == [0.1, 0.5, 0.9] and info["poe50_trace"] == 2
    assert info["trace_peaks"][4]["peak_kw"] == pytest.approx(523.0)


def test_yaml_data_source(bank_file):
    tg = TimeGrid(resolution_minutes=60, horizon_steps=2, start=datetime(2025, 7, 1, 0))
    out = resolve_array_param({"source": "loadcast", "path": str(bank_file), "trace": "poe50", "var": "net"}, tg)
    assert out.tolist() == pytest.approx([307.0, 308.0])


def test_example_feeder_scenario_reads_the_bank_trace(monkeypatch):
    from pathlib import Path

    from der_opt.scenarios import feeder_from_study, load_study_config

    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(root)
    built = feeder_from_study(load_study_config("examples/scenarios/feeder_bank_planning_trace.yaml"))
    element, load = built.background["existing_bank_load"]  # background load: fixed, not billed
    stamps = built.premises[0].time_grid.timestamps()
    peak = int(np.asarray(load).argmax())
    assert np.max(load) == pytest.approx(1990.4, abs=1.0)  # the design year's annual peak
    assert stamps[peak] == datetime(2026, 8, 4, 16)
    assert element == "bank" and "existing_bank_load" not in built.houses.index
