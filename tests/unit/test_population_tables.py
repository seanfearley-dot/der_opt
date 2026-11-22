"""The population tables, phase 2 (docs/POPULATION_MODEL_SPEC.md section 1, input data
spec 2.0): devices.csv, premise settings and tags, per-device and weather-station
series, templates by name, background rows, `der-opt convert premises`, and what
`validate` reports."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.inputs.spec import check_file, read_devices, read_premises
from der_opt.population.compile import PopulationError
from der_opt.population.convert import convert_premises
from der_opt.population.report import parameters_table, premises_table
from der_opt.scenarios import (
    build_feeder_scenario,
    load_feeder_config,
    run_feeder_study,
)
from der_opt.scenarios.schema import FeederScenarioConfig

REPO = Path(__file__).parent.parent.parent
DATA = REPO / "examples" / "data"
V11 = REPO / "tests" / "fixtures" / "v11"  # the examples' 1.1 premises files
runner = CliRunner()
STAMPS = pd.date_range("2026-07-13", periods=24, freq="h", tz="America/Los_Angeles")


def _series(premise: str, device: str = "", **columns) -> pd.DataFrame:
    frame = pd.DataFrame({"timestamp": [t.isoformat() for t in STAMPS], "premise_id": premise, "device_id": device})
    for name, values in columns.items():
        frame[name] = values if np.ndim(values) else [values] * 24
    return frame


def _population(folder: Path, devices_rows: str | None = None, premises_rows: str | None = None) -> Path:
    """A small 2.0 population: five premises, a background row, a weather file and a
    template folder. Returns the scenario file."""
    folder.mkdir(parents=True, exist_ok=True)
    pv = [0.0] * 7 + [1, 2, 3, 4, 4, 4, 3, 2, 1] + [0.0] * 8
    plugged = [1] * 7 + [0] * 11 + [1] * 6
    driving = [0.0] * 7 + [1.0] * 11 + [0.0] * 6
    series = pd.concat([
        _series("p1", load_kw=1.0), _series("p1", "solar", pv_kw=pv),
        _series("p1", "ev_1", ev_connected=plugged, ev_driving_kwh=driving),
        _series("p1", "ev_2", ev_connected=plugged, ev_driving_kwh=driving),
        _series("p2", load_kw=1.5),
        _series("p3", load_kw=0.8), _series("p3", "pump", flexible_kw=1.5),
        _series("p4", load_kw=1.2),
        _series("mall", load_kw=6.0),
    ], ignore_index=True).sort_values(["premise_id", "device_id", "timestamp"])
    series.to_csv(folder / "timeseries.csv", index=False)
    weather = pd.DataFrame({"timestamp": [t.isoformat() for t in STAMPS], "weather_station": "inland",
                            "outdoor_temp_c": [24 + 10 * np.sin(np.pi * h / 24) for h in range(24)]})
    weather.to_csv(folder / "weather.csv", index=False)
    (folder / "premises.csv").write_text(premises_rows or (
        "premise_id,element_id,kind,participation,baseline_behavior,response,weather_station,segment,load\n"
        "p1,tx,,,,,,existing,\n"
        "p2,tx,,non_participant,rule_based,,,existing,\n"
        "p3,tx,,,rule_based,rules,,new_build,\n"
        "p4,tx,,,,,inland,new_build,\n"
        "grid,tx,,,,,,,none\n"
        "mall,tx,background,,,,,,\n"), encoding="utf-8")
    (folder / "devices.csv").write_text(devices_rows or (
        "premise_id,device_id,type,template,kw,kwh,v2x,installed_cost,charge_sources,degradation_cost_per_kwh\n"
        "p1,solar,solar,,4,,,,,\n"
        "p1,ev_1,ev,,7.2,60,0,1200,,\n"
        "p1,ev_2,ev,,11.5,75,1,,,0.03\n"
        "p1,battery,battery,,5,13.5,,9000,solar,\n"
        "p2,ev,ev,commuter,7.2,60,0,,,\n"
        "p3,pump,flexible_load,,1.5,,,,,\n"
        "p4,ac,thermostat,cooling_ac,4,,,,,\n"
        "grid,battery,battery,,500,2000,,,,\n"), encoding="utf-8")
    templates = folder / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "commuter.yaml").write_text("""
type: ev
params:
  soc_init_frac: 0.8
  pattern: {weekday: {depart: "07:00", arrive: "18:00", miles: 30}, weekend: home, kwh_per_mile: 0.3}
counterfactual: {type: rule_based_ev}
""", encoding="utf-8")
    (templates / "cooling_ac.yaml").write_text("""
template: cooling_ac
type: thermostat
params: {thermal_resistance_r: 2.5, thermal_capacitance_c: 2.0, cop: 3.2, comfort_min_c: 20, comfort_max_c: 25,
         initial_temp_c: 22, mode: cooling}
counterfactual: {type: setpoint}
""", encoding="utf-8")
    flexible = templates / "flexible_two_blocks.yaml"
    flexible.write_text("type: flexible_load\nparams: {max_blocks: 2}\n", encoding="utf-8")
    scenario = folder / "scenario.yaml"
    scenario.write_text(yaml.safe_dump({
        "name": "tables",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 24, "start": "2026-07-13T00:00:00"},
        "feeder": {"elements": [{"name": "tx", "rating_kw": 60, "overload_cost_per_kwh": 1.0}]},
        "premise_data": {"premises": "premises.csv", "devices": "devices.csv",
                         "timeseries": ["timeseries.csv", "weather.csv"], "templates": ["templates"]},
        "house_defaults": {"value_streams": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "daily_tou", "hourly_pattern": [0.2] * 16 + [0.5] * 5 + [0.2] * 3},
            "export_price": {"type": "constant", "price": 0.05}}}], "poi_limit_kw": 30,
            "counterfactual": {"policies": {"ev": {"type": "rule_based_ev"}, "battery": {"type": "self_consumption"}}},
            "device_params": {"battery": {"degradation_cost_per_kwh": 0.015}}},
    }), encoding="utf-8")
    return scenario


@pytest.fixture
def population(tmp_path) -> Path:
    return _population(tmp_path / "pop")


def test_the_tables_resolve_every_device_kind(population):
    built = build_feeder_scenario(load_feeder_config(population))
    by = {r.premise_id: r for r in built.resolved}
    assert set(by) == {"p1", "p2", "p3", "p4", "grid"} and set(built.background) == {"mall"}
    p1 = {d.device_id: d for d in by["p1"].devices}
    assert set(p1) == {"solar", "ev_1", "ev_2", "battery"}  # two EVs behind one meter
    assert p1["ev_1"].params["p_max_discharge_kw"] == 0 and p1["ev_2"].params["p_max_discharge_kw"] == 11.5
    assert p1["ev_2"].params["degradation_cost_per_kwh"] == 0.03 and p1["ev_2"].sources["capacity_kwh"] == "devices.csv row 4"
    assert p1["battery"].params["charge_sources"] == ["solar"] and p1["battery"].costs == {"installed_cost": 9000}
    assert p1["battery"].sources["degradation_cost_per_kwh"] == "house_defaults.device_params.battery"
    assert p1["ev_1"].counterfactual == {"type": "rule_based_ev"}  # the default by type, for a device named ev_1
    ev = by["p2"].devices[0]
    assert ev.template == "commuter" and "pattern" in ev.params and ev.sources["soc_init_frac"] == "template:commuter"
    assert by["p2"].settings["participation"] == "non_participant" and by["p2"].sources["participation"] == "premises.csv row 3"
    pump = by["p3"].devices[0]
    assert pump.params["block_size_kw"] == 1.5 and pump.params["max_blocks"] == 1
    ac = by["p4"].devices[0]
    assert ac.params["ambient_temp_c"]["weather_station"] == "inland" and ac.counterfactual == {"type": "setpoint"}
    assert "external_load" not in by["grid"].settings  # load: none, a standalone battery
    assert by["p3"].extra["segment"] == "new_build"  # a tag, carried through
    study = run_feeder_study(built, counterfactual=False)
    assert not study.checks.errors, study.checks.errors
    assert study.runs["independent"].flows["tx"].iloc[0] > 6.0  # the mall's 6 kW is in every flow


def test_kw_on_a_flexible_load_spreads_over_its_blocks(tmp_path):
    rows = ("premise_id,device_id,type,template,kw\n"
            "p3,pump,flexible_load,flexible_two_blocks,1.5\n")
    scenario = _population(tmp_path / "pop", devices_rows=rows)
    built = build_feeder_scenario(load_feeder_config(scenario))
    pump = next(r for r in built.resolved if r.premise_id == "p3").devices[0]
    assert pump.params["max_blocks"] == 2 and pump.params["block_size_kw"] == 0.75


def test_the_tables_report_each_premise_and_parameter(population):
    built = build_feeder_scenario(load_feeder_config(population))
    table = premises_table(built.resolved, built.premises).set_index("premise_id")
    assert table.loc["p1", "n_ev"] == 2 and table.loc["p1", "ev_kwh"] == 135 and bool(table.loc["p1", "ev_v2x"])
    assert table.loc["p1", "installed_cost"] == 10200 and table.loc["p1", "solar_kw"] == 4.0
    assert table.loc["p4", "thermostat_kw"] == 4 and table.loc["p2", "settings_from"].startswith("participation: premises.csv")
    params = parameters_table(built.resolved)
    row = params[(params.premise_id == "p2") & (params.device_id == "ev") & (params.setting == "pattern.weekday.miles")]
    assert row["value"].item() == "30" and row["source"].item() == "template:commuter"


@pytest.mark.parametrize("devices_rows, message", [
    ("premise_id,device_id,type\np1,ev_1,ev\np1,ev_1,ev\n", "repeated premise_id + device_id"),
    ("premise_id,device_id,type\np1,x,heat_pump\n", "type 'heat_pump' isn't one of"),
    ("premise_id,device_id,type,colour\np1,ev_1,ev,red\n", "'colour' isn't a device parameter"),
    ("premise_id,device_id,type,cop\np1,ev_1,ev,3\n", "a ev has no parameter 'cop'"),
    ("premise_id,device_id,type\nmall,battery,battery\n", "devices on background rows"),
    ("premise_id,device_id,type,v2x\np1,solar,solar,1\n", "v2x is for EVs"),
])
def test_devices_file_errors(tmp_path, devices_rows, message):
    scenario = _population(tmp_path / "pop", devices_rows=devices_rows)
    premises = read_premises(scenario.parent / "premises.csv")
    report = read_devices(scenario.parent / "devices.csv", premises, strict=False).report
    assert any(message in f.message for f in report.errors), [f.message for f in report.errors]


def test_population_problems_are_all_reported_by_premise(tmp_path):
    rows = ("premise_id,device_id,type,template,kw,kwh,charge_sources\n"
            "p1,ev_9,ev,,7.2,60,\n"  # no plug-in series and no pattern
            "p2,ev,ev,nowhere,7.2,60,\n"  # unknown template
            "p3,solar,solar,,4,,\n"  # no pv_kw series
            "p4,battery,battery,,5,13.5,solar\n")  # charges from a device it doesn't have
    scenario = _population(tmp_path / "pop", devices_rows=rows)
    with pytest.raises(PopulationError) as caught:
        build_feeder_scenario(load_feeder_config(scenario))
    problems = caught.value.problems
    assert len(problems) == 4
    assert any("p1/ev_9: an EV needs ev_connected" in p for p in problems)
    assert any("p2/ev: no template 'nowhere'" in p for p in problems)
    assert any("p3/solar: a solar needs a pv_kw series" in p for p in problems)
    assert any("p4/battery: charges from ['solar']" in p for p in problems)
    result = runner.invoke(app, ["validate", str(scenario)])
    assert result.exit_code == 1 and result.output.count("ERROR population") == 1 and "4 premises" in result.output


def test_a_default_charge_source_a_premise_lacks_is_dropped(tmp_path):
    rows = "premise_id,device_id,type,kw,kwh\np4,battery,battery,5,13.5\n"
    scenario = _population(tmp_path / "pop", devices_rows=rows)
    config = load_feeder_config(scenario)
    config.house_defaults.device_params["battery"]["charge_sources"] = ["solar"]
    built = build_feeder_scenario(config)
    battery = next(r for r in built.resolved if r.premise_id == "p4").devices[0]
    assert "charge_sources" not in battery.params


def test_premise_settings_and_bill_checks(tmp_path):
    bad = ("premise_id,element_id,participation,response\n"
           "p1,tx,someday,rules\n")
    premises = _population(tmp_path / "a", premises_rows=bad).parent / "premises.csv"
    errors = [f.message for f in read_premises(premises, strict=False).report.errors]
    assert any("participation must be" in e for e in errors)
    assert any("needs baseline_behavior: rule_based" in e for e in errors)
    scenario = _population(tmp_path / "b")
    config = yaml.safe_load(scenario.read_text(encoding="utf-8"))
    config["house_defaults"]["value_streams"] = [{"type": "bill"}]  # a bill objective, but nobody has a tariff
    scenario.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(PopulationError, match="its objective is the bill, but it has no billing"):
        build_feeder_scenario(load_feeder_config(scenario))


def test_check_data_reads_devices_weather_and_v11_files(population):
    folder = population.parent
    kind, report, summary = check_file(folder / "devices.csv", premises=folder / "premises.csv")
    assert kind == "devices" and not report.errors and summary["by type"]["ev"] == 3
    kind, report, summary = check_file(folder / "weather.csv")
    assert kind == "time series" and not report.errors and summary["premises"] == "1 weather stations"
    kind, report, summary = check_file(folder / "premises.csv")
    assert summary["background rows"] == 1 and "segment" in summary["tags"]
    kind, report, _ = check_file(V11 / "spec_sample" / "premises.csv")
    assert any(f.check == "v1.1 device columns" and f.level == "warning" for f in report.findings)


@pytest.mark.parametrize("example, folder", [("feeder_from_files", "spec_sample"),
                                             ("feeder_ev_heat_wave", "heat_wave"),
                                             ("feeder_bank_planning_trace", "new_subdivision")])
def test_converting_v11_files_gives_identical_premises(tmp_path, example, folder):
    if "bank" in example:
        pytest.importorskip("xarray")
    converted = convert_premises(V11 / folder / "premises.csv", [DATA / folder / "timeseries.csv"], tmp_path)
    old = load_feeder_config(REPO / "tests" / "fixtures" / "old_forms" / f"{example}.yaml")
    new = FeederScenarioConfig.model_validate(old.model_dump(by_alias=True))
    new.premise_data.premises, new.premise_data.devices = str(converted.premises), str(converted.devices)
    a, b = build_feeder_scenario(old), build_feeder_scenario(new)
    assert [c.model_dump() for c in a.house_configs] == [c.model_dump() for c in b.house_configs]
    assert all(a.roles[k].__dict__ == b.roles[k].__dict__ for k in a.roles)
    assert "pv_kw_ac" not in pd.read_csv(converted.premises).columns


def test_cli_convert_validate_population_and_run_outputs(tmp_path, population):
    out = tmp_path / "converted"
    result = runner.invoke(app, ["convert", "premises", str(V11 / "spec_sample" / "premises.csv"),
                                 "--timeseries", str(DATA / "spec_sample" / "timeseries.csv"), "--out", str(out)])
    assert result.exit_code == 0 and "(1 battery, 3 ev, 2 solar)" in result.output
    tables = tmp_path / "tables"
    checked = runner.invoke(app, ["validate", str(population), "--population", str(tables)])
    assert checked.exit_code == 0, checked.output
    assert "tx: 5 premises" in checked.output and "2 thermostat" not in checked.output
    assert (tables / "premises.csv").exists() and (tables / "device_parameters.csv").exists()
    run = runner.invoke(app, ["run", str(population), "-o", str(tmp_path / "run"), "--no-counterfactual"])
    assert run.exit_code == 0, run.output
    assert (tmp_path / "run" / "device_parameters.csv").exists() and (tmp_path / "run" / "premises.csv").exists()
