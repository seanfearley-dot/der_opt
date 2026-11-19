"""Meter data ingestion. Each test writes a utility-style file from known true values
(15-minute energy, local-clock labels, both daylight-saving days), ingests it, and
checks the result against the truth."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.ingest import (
    IntervalSpec,
    check_series,
    ingest_frame,
    read_pge_interval_csv,
)

TZ = "America/Los_Angeles"
PREAMBLE = "Name,EXAMPLE CUSTOMER\nAddress,\"1 MAIN ST, ANYTOWN CA\"\nAccount Number,1234567890\nService,Service 1\n\n"


def _truth(start_local: str, days: int, minutes: int = 15) -> pd.Series:
    """kW by real (UTC) interval start: a daily shape, negative mid-day (solar export)."""
    start = pd.Timestamp(start_local, tz=TZ).tz_convert("UTC")
    end = (pd.Timestamp(start_local) + pd.Timedelta(days=days)).tz_localize(TZ).tz_convert("UTC")
    index = pd.date_range(start, end, freq=f"{minutes}min", inclusive="left")
    hour = index.tz_convert(TZ).hour + index.tz_convert(TZ).minute / 60
    return pd.Series(1.0 + np.cos((hour - 18) / 24 * 2 * np.pi) - 1.6 * np.exp(-((hour - 13) / 2.5) ** 2), index=index)


def _pge_file(path, truth: pd.Series, minutes: int = 15, split=False, gas_rows=0):
    local = truth.index.tz_convert(TZ)
    kwh = truth.to_numpy() * minutes / 60
    rows = {"TYPE": "Electric usage", "DATE": local.strftime("%Y-%m-%d"), "START TIME": local.strftime("%H:%M"),
            "END TIME": (local + pd.Timedelta(minutes=minutes - 1)).strftime("%H:%M")}
    if split:
        rows |= {"IMPORT (kWh)": np.clip(kwh, 0, None).round(4), "EXPORT (kWh)": np.clip(-kwh, 0, None).round(4)}
    else:
        rows |= {"USAGE (kWh)": kwh.round(4)}
    rows |= {"COST": "$0.00", "NOTES": ""}
    frame = pd.DataFrame(rows)
    if gas_rows:
        gas = frame.head(gas_rows).copy()
        gas["TYPE"] = "Natural gas usage"
        frame = pd.concat([frame, gas])
    path.write_text(PREAMBLE + frame.to_csv(index=False), encoding="utf-8")
    return path


def _hourly_local(truth: pd.Series) -> pd.Series:
    """What ingestion should produce: hourly means on the local clock, the repeated
    November hour averaged."""
    hourly = truth.resample("60min").mean().tz_convert(TZ)
    hourly.index = hourly.index.tz_localize(None)
    return hourly.groupby(level=0).mean()


def test_pge_signed_usage_over_the_november_change(tmp_path):
    truth = _truth("2026-10-31", 3)  # Nov 1 2026 has 25 hours
    result = read_pge_interval_csv(_pge_file(tmp_path / "pge.csv", truth), resolution_minutes=60)
    table = result.table
    assert len(table) == 72 and table.index.is_unique
    expected = _hourly_local(truth)
    np.testing.assert_allclose(table["net_kw"], expected.loc[table.index], atol=2e-3)
    assert (table["export_kw"] > 0).any() and (table[["import_kw", "export_kw"]] >= 0).all().all()
    checks = {f.check for f in result.report.findings}
    assert {"daylight saving", "channels"} <= checks
    assert "read as net" in result.summary["layout"]


def test_pge_import_and_export_columns_and_gas_rows(tmp_path):
    truth = _truth("2026-07-01", 2)
    result = read_pge_interval_csv(_pge_file(tmp_path / "pge.csv", truth, split=True, gas_rows=3))
    assert "separate import" in result.summary["layout"]
    assert len(result.table) == 2 * 96  # native 15-minute resolution
    np.testing.assert_allclose(result.table["net_kw"].to_numpy(), truth.to_numpy(), atol=2e-3)
    assert any("non-electric" in f.message for f in result.report.findings)


def test_march_skipped_hour_is_interpolated(tmp_path):
    truth = _truth("2026-03-07", 3)  # Mar 8 2026 has 23 hours
    table = read_pge_interval_csv(_pge_file(tmp_path / "pge.csv", truth), resolution_minutes=60).table
    assert len(table) == 72
    two_am = table.loc["2026-03-08 02:00", "net_kw"]
    assert two_am == pytest.approx((table.loc["2026-03-08 01:00", "net_kw"] + table.loc["2026-03-08 03:00", "net_kw"]) / 2)


def test_preamble_never_reaches_the_outputs(tmp_path):
    source = _pge_file(tmp_path / "pge.csv", _truth("2026-07-01", 1))
    out = tmp_path / "out" / "house.csv"
    result = CliRunner().invoke(app, ["ingest", str(source), "--pge", "-o", str(out), "--resolution-minutes", "60"])
    assert result.exit_code == 0, result.stdout
    for path in (out, out.with_name("house_report.json")):
        text = path.read_text(encoding="utf-8")
        assert "EXAMPLE CUSTOMER" not in text and "1234567890" not in text and "MAIN ST" not in text
    assert "EXAMPLE CUSTOMER" not in result.stdout
    assert json.loads(out.with_name("house_report.json").read_text(encoding="utf-8"))["summary"]["rows"] == 24


def _long_frame(truth: pd.Series, minutes=15, unit_factor=1.0):
    local = truth.index.tz_convert(TZ)
    end_labels = (local + pd.Timedelta(minutes=minutes)).tz_localize(None)  # interval-ending, local
    wh = truth.to_numpy() * minutes / 60 * unit_factor
    return pd.DataFrame({"READ_END": end_labels.astype(str), "SA": "M1",
                         "DEL": np.clip(wh, 0, None), "REC": np.clip(-wh, 0, None)})


def test_generic_long_file_hour_ending_wh_with_meter_choice():
    truth = _truth("2026-07-01", 1)
    frame = pd.concat([_long_frame(truth, unit_factor=1000), _long_frame(truth * 2, unit_factor=1000).assign(SA="M2")])
    spec = IntervalSpec(path="unused", unit="wh", meter="M1",
                        columns={"delivered": "DEL", "received": "REC", "meter_id": "SA"},
                        timestamp={"column": "READ_END", "tz": TZ, "convention": "ending"})
    table = ingest_frame(frame, spec).table
    assert table.index[0] == pd.Timestamp("2026-07-01 00:00")  # ending labels moved to interval start
    np.testing.assert_allclose(table["net_kw"].to_numpy(), truth.to_numpy(), atol=1e-6)
    with pytest.raises(ValueError, match="2 meters"):
        ingest_frame(frame, spec.model_copy(update={"meter": None}))


def test_wide_hour_ending_file_with_he25():
    truth = _truth("2026-10-31", 3, minutes=60)
    local = truth.index.tz_convert(TZ)
    day = local.tz_localize(None).normalize()
    hour_of_day = ((truth.index - pd.DatetimeIndex(day).tz_localize(TZ, ambiguous="NaT").tz_convert("UTC"))
                   / pd.Timedelta(hours=1)).astype(int)
    frame = pd.DataFrame({"DATE": day.strftime("%m/%d/%Y"), "col": [f"HE{h + 1}" for h in hour_of_day], "kwh": truth.to_numpy()})
    wide = frame.pivot(index="DATE", columns="col", values="kwh").reset_index()
    assert "HE25" in wide.columns  # the 25-hour day
    spec = IntervalSpec(path="unused", unit="kwh", wide_hours={"pattern": "HE{h}"},
                        timestamp={"date_column": "DATE", "format": "%m/%d/%Y", "tz": TZ})
    table = ingest_frame(wide, spec).table
    assert len(table) == 72
    np.testing.assert_allclose(table["net_kw"], _hourly_local(truth).loc[table.index], atol=1e-9)


def test_short_gaps_are_filled_and_long_ones_left_empty():
    truth = _truth("2026-07-01", 6)
    frame = _long_frame(truth)
    frame = frame.drop(index=[100]).drop(index=range(200, 200 + 3 * 96))  # one interval, then three days
    spec = IntervalSpec(path="unused", unit="kwh", columns={"delivered": "DEL", "received": "REC"},
                        timestamp={"column": "READ_END", "tz": TZ, "convention": "ending"})
    result = ingest_frame(frame, spec)
    table = result.table
    assert table["net_kw"].iloc[100] == pytest.approx(truth.iloc[100], abs=0.05)  # interpolated
    assert table["net_kw"].iloc[200:200 + 3 * 96].isna().all()  # not one invented interval
    messages = " ".join(f.message for f in result.report.findings)
    assert "1 missing intervals" in messages and "72 h" in messages


def test_check_series_flags_bad_data():
    index = pd.date_range("2026-07-01", periods=24 * 5, freq="h")
    good = pd.Series(1.0 + np.sin(np.arange(len(index)) / 3.8), index=index)
    assert check_series(good).findings == []

    outage = good.copy()
    outage.iloc[24:72] = 0.0
    flat = good.copy()
    flat.iloc[24:48] = 1.7
    spike = good.copy()
    spike.iloc[50] = 80.0
    watts = good * 1000 + 500
    for series, check in ((outage, "zero runs"), (flat, "flat runs"), (spike, "spikes"), (watts, "units")):
        assert check in {f.check for f in check_series(series).findings}, check

    doubled = pd.concat([good, good.iloc[:1]])
    assert "duplicates" in {f.check for f in check_series(doubled).findings}


def test_check_data_command(tmp_path):
    index = pd.date_range("2026-07-01", periods=48, freq="h")
    path = tmp_path / "load.csv"
    pd.DataFrame({"timestamp": index, "kw": 1.0 + np.arange(48) % 5}).drop(index=[10, 11, 12]).to_csv(path, index=False)
    result = CliRunner().invoke(app, ["check-data", str(path), "--column", "kw", "--resolution-minutes", "60"])
    assert result.exit_code == 0, result.stdout
    assert "regular steps" in result.stdout


def test_ingested_data_runs_in_a_scenario_without_input_warnings(tmp_path):
    from pathlib import Path

    example = Path(__file__).parent.parent.parent / "examples" / "ingest" / "meter_interval.yaml"
    house = tmp_path / "house.csv"
    runner = CliRunner()
    assert runner.invoke(app, ["ingest", str(example), "-o", str(house), "--resolution-minutes", "60"]).exit_code == 0
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(f"""
name: ingested_house
time_grid: {{resolution_minutes: 60, horizon_steps: 168, start: "2026-07-13T00:00:00"}}
devices:
  - {{type: battery, name: battery, params: {{capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5}}}}
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {{type: daily_tou, hourly_pattern: [0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5, 0.3, 0.3, 0.3]}}
      export_price: {{type: constant, price: 0.05}}
external_load: {{type: csv, path: {house}, column: net_kw}}
poi_limit_kw: 20
""", encoding="utf-8")
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code == 0, result.stdout
    assert "0 errors" in result.stdout and "input data" not in result.stdout
