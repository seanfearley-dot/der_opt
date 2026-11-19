"""LMP conversion to a scenario price CSV, and CAISO OASIS parsing, without the
network: OASIS responses are built here in its own layout."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.integrations import caiso_lmp
from der_opt.integrations.caiso_lmp import fetch_caiso_lmp, lmp_price_table

REPO = Path(__file__).parent.parent.parent


def _rows(start_utc: str, periods: int, minutes: int, price_fn, node="N1"):
    times = pd.date_range(start_utc, periods=periods, freq=f"{minutes}min", tz="UTC")
    return pd.DataFrame({"interval_start_utc": times, "node": node, "lmp_per_mwh": [price_fn(i) for i in range(periods)]})


def test_converts_units_and_clock_and_averages_down():
    # 5-minute prices from local midnight on a summer day (UTC-7), $/MWh = 100 + interval
    raw = _rows("2026-07-13 07:00", 24, 5, lambda i: 100 + i)
    table = lmp_price_table(raw, resolution_minutes=60)
    assert list(table.index) == [pd.Timestamp("2026-07-13 00:00"), pd.Timestamp("2026-07-13 01:00")]
    assert table["lmp_per_kwh"].tolist() == pytest.approx([(100 + 5.5) / 1000, (112 + 5.5) / 1000])


def test_november_repeated_hour_is_averaged():
    # 2025-11-02: 1 am happens twice (UTC-7 then UTC-8). Hourly prices = hour index.
    raw = _rows("2025-11-02 07:00", 25, 60, float)
    table = lmp_price_table(raw)
    assert len(table) == 24
    assert table.loc["2025-11-02 01:00", "lmp_per_kwh"] == pytest.approx((1 + 2) / 2 / 1000)
    assert table.loc["2025-11-02 02:00", "lmp_per_kwh"] == pytest.approx(3 / 1000)


def test_march_skipped_hour_repeats_the_hour_before():
    # 2026-03-08: 2 am doesn't exist (UTC-8 then UTC-7)
    raw = _rows("2026-03-08 08:00", 23, 60, float)
    table = lmp_price_table(raw)
    assert len(table) == 24
    assert table.loc["2026-03-08 02:00", "lmp_per_kwh"] == table.loc["2026-03-08 01:00", "lmp_per_kwh"]
    assert table.loc["2026-03-08 03:00", "lmp_per_kwh"] == pytest.approx(2 / 1000)


def test_coarser_data_is_held_across_finer_steps():
    raw = _rows("2026-07-13 07:00", 2, 60, lambda i: 40.0 + 10 * i)
    table = lmp_price_table(raw, resolution_minutes=15)
    assert table["lmp_per_kwh"].tolist() == pytest.approx([0.04, 0.04, 0.04, 0.04, 0.05])


def test_one_node_must_be_chosen():
    raw = pd.concat([_rows("2026-07-13 07:00", 2, 60, float, "A"), _rows("2026-07-13 07:00", 2, 60, lambda i: 9.0, "B")])
    with pytest.raises(ValueError, match="2 pricing nodes"):
        lmp_price_table(raw)
    assert lmp_price_table(raw, node="B")["lmp_per_kwh"].tolist() == pytest.approx([0.009, 0.009])
    with pytest.raises(ValueError, match="isn't in the data"):
        lmp_price_table(raw, node="C")


def test_other_column_names_such_as_gridstatus():
    raw = _rows("2026-07-13 07:00", 2, 60, float).rename(
        columns={"interval_start_utc": "Interval Start", "node": "Location", "lmp_per_mwh": "LMP"}
    )
    raw["Interval Start"] = raw["Interval Start"].dt.tz_convert("US/Pacific")
    table = lmp_price_table(raw, time_column="Interval Start", node_column="Location", price_column="LMP")
    assert table.index[0] == pd.Timestamp("2026-07-13 00:00")


# --- OASIS --------------------------------------------------------------------------


def _oasis_zip(start_utc: str, hours: int, price_column="MW") -> bytes:
    rows = []
    for h in range(hours):
        t = pd.Timestamp(start_utc, tz="UTC") + pd.Timedelta(hours=h)
        for component, share in (("LMP", 1.0), ("MCE", 0.9), ("MCC", 0.05), ("MCL", 0.05)):
            rows.append({"INTERVALSTARTTIME_GMT": t.strftime("%Y-%m-%dT%H:%M:%S-00:00"), "NODE": "N1",
                         "LMP_TYPE": component, price_column: (50.0 + h) * share})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.csv", pd.DataFrame(rows).sample(frac=1, random_state=0).to_csv(index=False))
    return buffer.getvalue()


class FakeOasis:
    def __init__(self, content_fn):
        self.requests = []
        self.content_fn = content_fn

    def get(self, url, params, timeout):
        self.requests.append(params)

        class Response:
            content = self.content_fn(params)

            def raise_for_status(self):
                return None

        return Response()


def _days_requested(params):
    start = pd.Timestamp(params["startdatetime"].replace("-0000", ""), tz="UTC")
    end = pd.Timestamp(params["enddatetime"].replace("-0000", ""), tz="UTC")
    return start, int((end - start) / pd.Timedelta(hours=1))


def test_fetch_keeps_total_lmp_sorted_and_splits_long_ranges():
    fake = FakeOasis(lambda p: _oasis_zip(_days_requested(p)[0].strftime("%Y-%m-%d %H:%M"), _days_requested(p)[1]))
    raw = fetch_caiso_lmp("N1", date(2026, 7, 1), date(2026, 8, 5), session=fake, pause_s=0)
    assert len(fake.requests) == 2  # 35 days at 30 days per request
    assert fake.requests[0]["startdatetime"] == "20260701T07:00-0000"  # local midnight, PDT
    assert len(raw) == 35 * 24 and raw["interval_start_utc"].is_monotonic_increasing
    assert raw["lmp_per_mwh"].iloc[0] == 50.0  # the LMP row, not a component


def test_fetch_reports_oasis_errors():
    error = io.BytesIO()
    with zipfile.ZipFile(error, "w") as archive:
        archive.writestr("err.xml", "<m:ERROR><m:ERR_CODE>1004</m:ERR_CODE><m:ERR_DESC>range too long</m:ERR_DESC></m:ERROR>")
    with pytest.raises(RuntimeError, match="1004 range too long"):
        fetch_caiso_lmp("N1", date(2026, 7, 1), date(2026, 7, 2), session=FakeOasis(lambda p: error.getvalue()))


def test_fetch_lmp_command_writes_a_csv_a_scenario_can_price_with(tmp_path, monkeypatch):
    def fake_fetch(node, start, end, market):
        return _rows("2026-07-13 07:00", 48, 60, lambda i: 30.0 + i, node)

    monkeypatch.setattr(caiso_lmp, "fetch_caiso_lmp", fake_fetch)
    out = tmp_path / "lmp.csv"
    result = CliRunner().invoke(app, ["fetch-lmp", "N1", "2026-07-13", "2026-07-15", "-o", str(out)])
    assert result.exit_code == 0, result.stdout

    from der_opt.pricing import CSVPriceSeries
    from der_opt.scenarios.loader import _build_price_series
    from der_opt.time_grid import TimeGrid

    series = _build_price_series({"type": "csv", "path": str(out), "column": "lmp_per_kwh"})
    assert isinstance(series, CSVPriceSeries)
    values = series.values(TimeGrid(60, 48, pd.Timestamp("2026-07-13").to_pydatetime()))
    np.testing.assert_allclose(values, (30.0 + np.arange(48)) / 1000, atol=1e-6)


def test_lmp_battery_example_runs():
    result = CliRunner().invoke(app, ["run", str(REPO / "examples" / "scenarios" / "battery_caiso_lmp.yaml")])
    assert result.exit_code == 0, result.stdout
    assert "EnergyArbitrage: $-" in result.stdout  # it earns money
