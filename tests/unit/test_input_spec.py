"""The input data spec v1.0 (docs/INPUT_DATA_SPEC.md): every rejection rule, the
daylight-saving days, cross-file checks, and running scenarios from spec files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.inputs import (
    SpecError,
    check_file,
    check_series,
    read_network,
    read_premises,
    read_timeseries,
)

REPO = Path(__file__).parent.parent.parent
SAMPLE = REPO / "examples" / "data" / "spec_sample"
runner = CliRunner()


def _write(tmp_path, text: str, name="data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _series_text(hours=4, start="2026-07-13T00:00:00-07:00", premise="h1", column="load_kw", value=1.0):
    stamps = pd.date_range(pd.Timestamp(start), periods=hours, freq="h")
    rows = [f"{t.isoformat()},{premise},{value}" for t in stamps]
    return "timestamp,premise_id," + column + "\n" + "\n".join(rows)


def _errors(path) -> str:
    _, report, _ = check_file(path)
    return " | ".join(f.message for f in report.errors)


# --- the sample and the example -----------------------------------------------------


def test_sample_files_pass_and_the_feeder_example_runs(tmp_path):
    for name in ("timeseries", "premises", "network"):
        _, report, _ = check_file(SAMPLE / f"{name}.csv", premises=SAMPLE / "premises.csv", network=SAMPLE / "network.csv")
        assert report.errors == [] and report.warnings == [], name
    result = runner.invoke(app, ["run", str(REPO / "examples/scenarios/feeder_from_files.yaml"), "-o", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "6 houses on 3 network elements" in result.stdout and " 0 errors" in result.stdout
    houses = pd.read_csv(tmp_path / "houses.csv", index_col=0)
    assert houses.loc["home_05", "data"] == "metered" and houses.loc["home_06", "export_program"] == "nem2"


# --- time series rules ----------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("timestamp,premise_id,load_kw\n2026-07-13 00:00,h1,1\n2026-07-13 01:00,h1,1", "UTC offset"),
    ("Name,Someone\ntimestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1", "header"),
    ("Timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1\n2026-07-13T01:00:00-07:00,h1,1", "rename column"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,\"1,200\"\n2026-07-13T01:00:00-07:00,h1,1", "plain numbers"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,-1\n2026-07-13T01:00:00-07:00,h1,1", "negative"),
    ("timestamp,premise_id,ev_connected\n2026-07-13T00:00:00-07:00,h1,2\n2026-07-13T01:00:00-07:00,h1,1", "0 or 1"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1\n2026-07-13T00:00:00-07:00,h1,2", "repeated"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1\n2026-07-13T00:07:00-07:00,h1,1", "7-minute"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1\n2026-07-13T01:00:00-07:00,h1,1\n"
     "2026-07-13T03:00:00-07:00,h1,1", "rows are missing"),
    ("timestamp,premise_id,load_kw,import_kw\n2026-07-13T00:00:00-07:00,h1,1,\n2026-07-13T01:00:00-07:00,h1,,1", "both gross"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h 1,1\n2026-07-13T01:00:00-07:00,h 1,1", "characters"),
    ("timestamp,premise_id,load_kw\n2026-07-13T00:00:00-07:00,h1,1\n\n2026-07-13T01:00:00-07:00,h1,1", "blank lines"),
])
def test_time_series_rejections(tmp_path, text, expected):
    path = _write(tmp_path, text)
    assert expected in _errors(path)
    with pytest.raises(SpecError, match="doesn't meet the input data spec"):
        read_timeseries(path)


def test_different_intervals_per_premise_are_rejected(tmp_path):
    a = _series_text(4, premise="a")
    stamps = pd.date_range(pd.Timestamp("2026-07-13T00:00:00-07:00"), periods=8, freq="30min")
    b = "\n".join(f"{t.isoformat()},b,1" for t in stamps)
    assert "different intervals" in _errors(_write(tmp_path, a + "\n" + b))


def test_kwh_named_column_is_flagged_and_unknown_columns_ignored(tmp_path):
    text = _series_text().replace("load_kw", "load_kw,load_kwh,my_note")
    text = "\n".join(line if i == 0 else line + ",0.5,hello" for i, line in enumerate(text.splitlines()))
    _, report, _ = check_file(_write(tmp_path, text))
    assert report.errors == []
    assert any("load_kwh" in f.message for f in report.warnings)


def test_november_repeated_hour_and_march_skipped_hour(tmp_path):
    fall = pd.date_range("2026-11-01 00:00", periods=25, freq="h", tz="UTC").map(
        lambda t: t + pd.Timedelta(hours=7)).tz_convert("America/Los_Angeles")
    text = "timestamp,load_kw\n" + "\n".join(f"{t.isoformat()},{i}" for i, t in enumerate(fall))
    assert text.count("T01:00:00-07:00") == 1 and text.count("T01:00:00-08:00") == 1  # the hour, twice
    data = read_timeseries(_write(tmp_path, text, "fall.csv"))
    local = data.local_series("load_kw")
    assert len(local) == 24 and local.loc["2026-11-01 01:00"] == pytest.approx(1.5)
    assert any(f.level == "info" for f in data.report.findings)

    spring = pd.date_range("2026-03-08 08:00", periods=23, freq="h", tz="UTC").tz_convert("America/Los_Angeles")
    text = "timestamp,load_kw\n" + "\n".join(f"{t.isoformat()},{i}" for i, t in enumerate(spring))
    local = read_timeseries(_write(tmp_path, text, "spring.csv")).local_series("load_kw")
    assert len(local) == 24 and local.loc["2026-03-08 02:00"] == pytest.approx(1.5)


def test_empty_cells_warn_and_surface_again_at_run_time(tmp_path):
    text = _series_text(48).splitlines()
    text[10] = text[10].rsplit(",", 1)[0] + ","
    path = _write(tmp_path, "\n".join(text))
    _, report, _ = check_file(path)
    assert report.errors == [] and any("1 empty cells" in f.message for f in report.warnings)


def test_shared_price_file_and_single_premise_scenario(tmp_path):
    loads = _write(tmp_path, _series_text(24, value=2.0), "load.csv")
    stamps = pd.date_range(pd.Timestamp("2026-07-13T00:00:00-07:00"), periods=24, freq="h")
    prices = _write(tmp_path, "timestamp,import_price_per_kwh,export_price_per_kwh\n" + "\n".join(
        f"{t.isoformat()},{0.5 if 16 <= t.hour < 21 else 0.2},0.05" for t in stamps), "prices.csv")
    scenario = _write(tmp_path, f"""
name: one_house
time_grid: {{resolution_minutes: 60, horizon_steps: 24, start: "2026-07-13T00:00:00"}}
devices:
  - {{type: battery, name: battery, params: {{capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5}}}}
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {{type: timeseries, path: {prices}, column: import_price_per_kwh}}
      export_price: {{type: timeseries, path: {prices}, column: export_price_per_kwh}}
external_load: {{source: timeseries, path: {loads}, premise_id: h1, column: load_kw}}
poi_limit_kw: 20
""", "scenario.yaml")
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code == 0, result.stdout
    assert "0 errors" in result.stdout and "input data" not in result.stdout

    late = scenario.read_text(encoding="utf-8").replace('start: "2026-07-13T00:00:00"', 'start: "2026-07-13T12:00:00"')
    result = runner.invoke(app, ["run", str(_write(tmp_path, late, "late.yaml"))])
    assert result.exit_code != 0 and "not the whole run" in str(result.exception)


# --- premises and network -----------------------------------------------------------


def test_premises_rules(tmp_path):
    network = read_network(SAMPLE / "network.csv")
    path = _write(tmp_path, """
premise_id,element_id,battery_kwh,battery_kw,ev_v2x,export_program,customer_class
a,tx_1,13.5,,0,nbt,residential
a,tx_9,,,2,net_metering,industrial
""", "premises.csv")
    messages = " | ".join(f.message for f in read_premises(path, network, strict=False).report.errors)
    for expected in ("repeated premise_id", "battery_kwh and battery_kw go together", "ev_v2x must be",
                     "export_program must be", "isn't in network.csv", "customer_class must be"):
        assert expected in messages, expected


def test_network_rules(tmp_path):
    loop = _write(tmp_path, "element_id,parent_id,rating_kw\na,b,10\nb,a,10", "loop.csv")
    assert "loops back" in _errors(loop)
    orphan = _write(tmp_path, "element_id,parent_id,rating_kw\na,,10\na,x,-5", "orphan.csv")
    messages = _errors(orphan)
    assert "repeated element_id" in messages and "parent_id not among" in messages and "negative" in messages


def test_check_data_command_exit_codes(tmp_path):
    good = runner.invoke(app, ["check-data", str(SAMPLE / "timeseries.csv"), "--premises", str(SAMPLE / "premises.csv")])
    assert good.exit_code == 0 and "Passes the spec" in good.stdout
    bad = _write(tmp_path, "timestamp,premise_id,load_kw\n2026-07-13 00:00,h1,1")
    result = runner.invoke(app, ["check-data", str(bad)])
    assert result.exit_code == 1 and "Rejected" in result.stdout
    unknown = _write(tmp_path, _series_text(4, premise="stranger"), "stranger.csv")
    result = runner.invoke(app, ["check-data", str(unknown), "--premises", str(SAMPLE / "premises.csv")])
    assert result.exit_code == 1 and "stranger" in result.stdout


def test_premise_data_refuses_devices_on_metered_premises(tmp_path):
    from der_opt.scenarios import build_feeder_scenario
    from der_opt.scenarios.schema import FeederScenarioConfig

    premises = _write(tmp_path, "premise_id,element_id,battery_kwh,battery_kw\nm1,tx,10,5", "premises.csv")
    series = _write(tmp_path, _series_text(24, premise="m1", column="import_kw"), "ts.csv")
    config = FeederScenarioConfig.model_validate({
        "name": "x", "time_grid": {"resolution_minutes": 60, "horizon_steps": 24, "start": "2026-07-13T00:00:00"},
        "feeder": {"elements": [{"name": "tx"}]},
        "premise_data": {"premises": str(premises), "timeseries": [str(series)]},
    })
    with pytest.raises(ValueError, match="metered data"):
        build_feeder_scenario(config)


def test_bill_stream_follows_the_program():
    from der_opt.billing.objective import NBTBill, NEM2Bill
    from der_opt.scenarios import build_premise
    from der_opt.scenarios.schema import ScenarioConfig

    base = {"name": "x", "time_grid": {"resolution_minutes": 60, "horizon_steps": 24, "start": "2026-07-13T00:00:00"},
            "external_load": [1.0] * 24, "poi_limit_kw": 10, "value_streams": [{"type": "bill"}]}
    nem2 = ScenarioConfig.model_validate(base | {"billing": {"tariff": str(REPO / "examples/data/tariffs/pge_e_tou_c_2026-06.yaml"),
                                                             "baseline_territory": "R", "program": {"type": "nem2"}}})
    assert isinstance(build_premise(nem2).value_streams[0], NEM2Bill)
    nbt = ScenarioConfig.model_validate(base | {"billing": {"tariff": str(REPO / "examples/data/tariffs/pge_e_elec_2026-06.yaml")}})
    assert isinstance(build_premise(nbt).value_streams[0], NBTBill)
    bad = ScenarioConfig.model_validate(base | {"billing": {"tariff": str(REPO / "examples/data/tariffs/pge_e_tou_c_2026-06.yaml"),
                                                            "baseline_territory": "R"}})
    with pytest.raises(ValueError, match="baseline credit"):
        build_premise(bad)


# --- quality checks -----------------------------------------------------------------


def test_quality_checks_flag_bad_data():
    index = pd.date_range("2026-07-01", periods=24 * 5, freq="h")
    good = pd.Series(1.0 + np.sin(np.arange(len(index)) / 3.8), index=index)
    assert check_series(good).findings == []
    outage, flat, spike = good.copy(), good.copy(), good.copy()
    outage.iloc[24:72] = 0.0
    flat.iloc[24:48] = 1.7
    spike.iloc[50] = 80.0
    for series, check in ((outage, "zero runs"), (flat, "flat runs"), (spike, "spikes"), (good * 1000 + 500, "units")):
        assert check in {f.check for f in check_series(series).findings}, check
    assert "units" in {f.check for f in check_series(good + 150, home_peak_kw=100).findings}
