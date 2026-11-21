"""Scenario building blocks (docs/SCENARIO_COMPOSITION_SPEC.md v1.0): `extends`, paths
relative to the file that names them, `der-opt validate` and `der-opt new`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.scenarios import load_scenario, load_scenario_config
from der_opt.scenarios.compose import (
    CompositionError,
    OldStylePathWarning,
    compose_scenario,
    merge,
)
from der_opt.scenarios.validate import validate_scenario

REPO = Path(__file__).parent.parent.parent
DATA = REPO / "examples" / "data"
runner = CliRunner()


def _flat(output: str) -> str:
    return " ".join(output.replace("│", " ").replace("╭", " ").replace("╰", " ").split())


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


BASE = """
time_grid: {resolution_minutes: 60, horizon_steps: 4, start: "2026-06-01T00:00:00"}
devices:
  - {type: battery, name: battery, params: {capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5,
                                             charge_sources: [solar]}}
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {type: constant, price: 0.2}
      export_price: {type: constant, price: 0.05}
poi_limit_kw: 20
counterfactual: {policies: {battery: {type: self_consumption}}}
"""


# --- merging --------------------------------------------------------------------------


def test_merge_rules():
    base = {"a": {"x": 1, "y": 2}, "devices": [{"name": "battery", "p": 1}, {"name": "ev", "p": 2}],
            "value_streams": [{"type": "one"}], "counterfactual": {"policies": {}}}
    over = {"a": {"y": 3}, "devices": [{"name": "battery", "p": 9}, {"name": "solar", "p": 0}],
            "value_streams": [{"type": "two"}], "counterfactual": None}
    merged = merge(base, over)
    assert merged["a"] == {"x": 1, "y": 3}  # mappings merge key by key
    assert merged["devices"] == [{"name": "battery", "p": 9}, {"name": "ev", "p": 2}, {"name": "solar", "p": 0}]
    # a device with an inherited name merges into it setting by setting; null removes one
    block = {"devices": [{"type": "battery", "name": "battery", "params": {"eta_charge": 0.95, "soc_final_min_frac": 0.5}}]}
    tweak = {"devices": [{"name": "battery", "params": {"eta_charge": 0.96, "soc_final_min_frac": None}}]}
    assert merge(block, tweak)["devices"] == [{"type": "battery", "name": "battery", "params": {"eta_charge": 0.96}}]
    other = {"devices": [{"type": "ev_v2x", "name": "battery", "params": {}}]}
    assert merge(block, other)["devices"] == other["devices"]  # another type replaces it
    assert merged["value_streams"] == [{"type": "two"}]  # other lists replace
    assert "counterfactual" not in merged  # null removes an inherited key
    assert merge({}, {"poi_limit_kw": None}) == {"poi_limit_kw": None}  # nothing inherited: passed through


def test_extends_merges_in_order_and_fragments_can_extend(tmp_path):
    _write(tmp_path / "lib" / "time.yaml", 'time_grid: {resolution_minutes: 60, horizon_steps: 4, start: "2026-06-01T00:00:00"}\n')
    without_time = "\n".join(line for line in BASE.splitlines() if not line.startswith("time_grid"))
    _write(tmp_path / "lib" / "base.yaml", "extends: time.yaml\n" + without_time)
    scenario = _write(tmp_path / "scen" / "home.yaml", """
extends: [../lib/base.yaml]
name: home
devices:
  - {type: solar, name: solar, params: {available_kw: [0, 2, 5, 1]}}
external_load: [1, 1, 3, 3]
counterfactual: null
""")
    composed = compose_scenario(scenario)
    assert [p.name for p in composed.fragments] == ["time.yaml", "base.yaml"]
    premise = load_scenario(scenario)
    # the battery fragment comes first and charges from the scenario's own solar
    assert [d.name for d in premise.devices] == ["battery", "solar"]
    assert premise.devices[0].charge_sources == [premise.devices[1]]
    assert load_scenario_config(scenario).counterfactual is None


def test_extends_errors(tmp_path):
    _write(tmp_path / "a.yaml", "extends: b.yaml\nname: a\n")
    _write(tmp_path / "b.yaml", "extends: a.yaml\n")
    with pytest.raises(CompositionError, match="circle: a.yaml -> b.yaml -> a.yaml"):
        compose_scenario(tmp_path / "a.yaml")
    _write(tmp_path / "c.yaml", "extends: [nowhere.yaml]\n")
    with pytest.raises(CompositionError, match="extends 'nowhere.yaml', which doesn't exist"):
        compose_scenario(tmp_path / "c.yaml")
    _write(tmp_path / "d.yaml", "- just a list\n")
    with pytest.raises(CompositionError, match="must be a mapping"):
        compose_scenario(tmp_path / "d.yaml")


# --- paths ----------------------------------------------------------------------------


def _home_with_files(root: Path) -> Path:
    """A scenario in root/scen that reads root/data/prices.csv and a fragment in root/lib
    that names a tariff in root/tariffs."""
    shutil.copy(DATA / "caiso_lmp_np15_dam_2026-07-13.csv", _write(root / "data" / "prices.csv", ""))
    shutil.copy(DATA / "tariffs" / "pge_e_elec_2026-06.yaml", _write(root / "tariffs" / "e_elec.yaml", ""))
    _write(root / "lib" / "billing.yaml", "billing: {tariff: ../tariffs/e_elec.yaml}\n")
    return _write(root / "scen" / "lmp.yaml", """
extends: ../lib/billing.yaml
name: lmp
time_grid: {resolution_minutes: 60, horizon_steps: 24, start: "2026-07-13T00:00:00"}
devices:
  - {type: battery, name: battery, params: {capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5}}
value_streams:
  - type: energy_arbitrage
    params:
      import_price: {type: csv, path: ../data/prices.csv, column: lmp_per_kwh}
      export_price: {type: csv, path: ../data/prices.csv, column: lmp_per_kwh}
poi_limit_kw: 20
""")


def test_paths_resolve_from_the_file_that_names_them(tmp_path, monkeypatch):
    scenario = _home_with_files(tmp_path / "project")
    monkeypatch.chdir(tmp_path)  # anywhere but the scenario's folder
    config = load_scenario_config(scenario)
    assert Path(config.billing.tariff) == (tmp_path / "project" / "tariffs" / "e_elec.yaml").resolve()
    price = config.value_streams[0].params["import_price"]["path"]
    assert Path(price) == (tmp_path / "project" / "data" / "prices.csv").resolve()
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code == 0, result.output


def test_old_style_paths_still_work_for_now_with_a_warning(tmp_path, monkeypatch):
    project = tmp_path / "project"
    scenario = _home_with_files(project)
    old = scenario.read_text(encoding="utf-8").replace("../data/prices.csv", "data/prices.csv")
    _write(scenario, old)
    monkeypatch.chdir(project)  # data/prices.csv exists from here, not from scen/
    with pytest.warns(OldStylePathWarning, match="write it as '../data/prices.csv'"):
        config = load_scenario_config(scenario)
    assert Path(config.value_streams[0].params["import_price"]["path"]).exists()
    report = validate_scenario(scenario).report
    assert any(f.check == "paths" and f.level == "warning" for f in report.findings) and not report.errors


def test_examples_use_file_relative_paths():
    for scenario in sorted((REPO / "examples" / "scenarios").glob("*.yaml")):
        assert compose_scenario(scenario).path_warnings == [], scenario.name


# --- the run record -------------------------------------------------------------------


def test_run_records_fragments_and_writes_the_resolved_scenario(tmp_path):
    scenario = _home_with_files(tmp_path / "project")
    out = tmp_path / "out"
    assert runner.invoke(app, ["run", str(scenario), "-o", str(out)]).exit_code == 0
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    (fragment,) = record["scenario"]["fragments"]
    assert fragment["path"].endswith("billing.yaml") and len(fragment["sha256"]) == 64
    resolved = yaml.safe_load((out / "scenario.resolved.yaml").read_text(encoding="utf-8"))
    assert "extends" not in resolved and Path(resolved["billing"]["tariff"]).is_absolute()
    # the resolved scenario runs as it is, from anywhere
    again = runner.invoke(app, ["run", str(out / "scenario.resolved.yaml")])
    assert again.exit_code == 0, again.output


# --- validate -------------------------------------------------------------------------


def test_validate_passes_an_example_and_reports_problems_without_solving(tmp_path):
    good = runner.invoke(app, ["validate", str(REPO / "examples/scenarios/dr_battery_annual.yaml")])
    assert good.exit_code == 0 and "Ready to run." in good.output, good.output

    scenario = _home_with_files(tmp_path / "p")
    text = scenario.read_text(encoding="utf-8")
    _write(scenario, text.replace("../data/prices.csv", "../data/missing.csv", 1))
    missing = runner.invoke(app, ["validate", str(scenario)])
    assert missing.exit_code == 1
    assert "value_streams[0].params.import_price.path" in missing.output and "doesn't exist" in missing.output

    _write(scenario, text.replace("horizon_steps: 24", "horizon_steps: lots"))
    schema = runner.invoke(app, ["validate", str(scenario)])
    assert schema.exit_code == 1 and "ERROR schema: time_grid.horizon_steps" in schema.output

    _write(scenario, text.replace("p_max_discharge_kw: 5}", "p_max_discharge_kw: 5, charge_sources: [solar]}"))
    build = runner.invoke(app, ["validate", str(scenario)])
    assert build.exit_code == 1 and "ERROR build" in build.output and "charges from ['solar']" in build.output


def test_validate_checks_names_calendars_and_the_measure_life(tmp_path):
    # the example with its paths resolved, so a changed copy can live anywhere
    example = compose_scenario(REPO / "examples/scenarios/dr_battery_annual.yaml").data

    def check(change) -> list[str]:
        data = json.loads(json.dumps(example))
        change(data)
        path = _write(tmp_path / "changed.yaml", yaml.safe_dump(data))
        return [f"{f.check}: {f.message}" for f in validate_scenario(path).report.errors]

    assert check(lambda d: None) == []
    assert any("cost_test.measure_devices names ['batery']" in e
               for e in check(lambda d: d["cost_test"].update(scope="device", measure_devices=["batery"])))
    assert any("dr calendar" in e and "outside the program's season" in e for e in check(
        lambda d: d["dr"]["events"].update(random=None, schedule=[{"start": "17:00", "hours": 2, "dates": ["2026-01-15"]}])))
    assert any("life runs to 2029" in e and "not 2020" in e
               for e in check(lambda d: d["cost_test"].update(start_year=2020)))


def test_run_validates_first_and_no_validate_skips_it(tmp_path):
    scenario = _home_with_files(tmp_path / "p")
    _write(scenario, scenario.read_text(encoding="utf-8").replace(
        "p_max_discharge_kw: 5}", "p_max_discharge_kw: 5, charge_sources: [solar]}"))
    out = tmp_path / "out"
    stopped = runner.invoke(app, ["run", str(scenario), "-o", str(out)])
    assert stopped.exit_code == 1 and "didn't validate, so nothing was solved" in stopped.output
    assert not out.exists()
    skipped = runner.invoke(app, ["run", str(scenario), "--no-validate"])
    assert skipped.exit_code != 0 and "didn't validate" not in skipped.output
    assert "charges from ['solar']" in str(skipped.exception)  # the same error, found the slow way


# --- new ------------------------------------------------------------------------------


def test_new_lists_templates():
    result = runner.invoke(app, ["new", "--list"])
    assert result.exit_code == 0
    for name in ("dr-battery", "home-bill", "feeder-from-files", "lmp-battery"):
        assert name in result.output


def test_new_dr_battery_with_a_library_validates_once_filled_in(tmp_path):
    project = tmp_path / "project"
    for name in ("spec_sample/annual_home.csv", "tariffs/pge_e_elec_2026-06.yaml"):
        _write(project / "data" / name, "")
        shutil.copy(DATA / name, project / "data" / name)
    shutil.copytree(DATA / "avoided_costs" / "synthetic_2026", project / "data" / "synthetic_2026")
    args = ["--timeseries", str(project / "data/spec_sample/annual_home.csv"),
            "--tariff", str(project / "data/tariffs/pge_e_elec_2026-06.yaml"),
            "--avoided-costs", str(project / "data/synthetic_2026"), "--library", str(project / "library")]
    first = runner.invoke(app, ["new", "dr-battery", str(project / "scen" / "home_a.yaml"), *args])
    assert first.exit_code == 0, first.output
    assert "5 TODOs to fill in" in first.output and first.output.count("new fragment") == 7
    scenario = project / "scen" / "home_a.yaml"
    text = scenario.read_text(encoding="utf-8")
    assert "path: ../data/spec_sample/annual_home.csv, premise_id: home_a" in text  # relative, premise found
    assert "- ../library/programs/summer_peak_incentive.yaml" in text

    todo = runner.invoke(app, ["validate", str(scenario)])
    assert todo.exit_code == 1 and "in summer_peak_incentive.yaml; fill it in" in todo.output
    program = project / "library" / "programs" / "summer_peak_incentive.yaml"
    program.write_text(program.read_text(encoding="utf-8").replace("incentive_per_kwh: TODO", "incentive_per_kwh: 1.0"),
                       encoding="utf-8")
    costs = project / "library" / "cost_tests" / "synthetic_2026.yaml"
    costs.write_text(costs.read_text(encoding="utf-8").replace("upfront: TODO", "upfront: 300").replace(
        "utility: TODO, participant: TODO, societal: TODO", "utility: 0.075, participant: 0.1, societal: 0.03"),
        encoding="utf-8")
    ready = runner.invoke(app, ["validate", str(scenario)])
    assert ready.exit_code == 0, ready.output

    # a second home reuses the library as it is
    second = runner.invoke(app, ["new", "dr-battery", str(project / "scen" / "home_b.yaml"), *args])
    assert second.output.count("uses existing fragment") == 7 and "new fragment" not in second.output
    assert "incentive_per_kwh: 1.0" in program.read_text(encoding="utf-8")
    again = runner.invoke(app, ["new", "dr-battery", str(scenario), *args])
    assert again.exit_code != 0 and "--force" in _flat(again.output)


@pytest.mark.parametrize("template, args", [
    ("home-bill", ["--timeseries", "spec_sample/annual_home.csv", "--tariff", "tariffs/pge_e_elec_2026-06.yaml",
                   "--export-rates", "pge_nbt_eec_2026.csv"]),
    ("lmp-battery", ["--price", "caiso_lmp_np15_dam_2026-07-13.csv"]),
])
def test_new_templates_that_need_nothing_else_validate(tmp_path, template, args):
    args = [str(DATA / a) if not a.startswith("--") else a for a in args]
    target = tmp_path / "scenario.yaml"
    result = runner.invoke(app, ["new", template, str(target), *args])
    assert result.exit_code == 0 and "TODO" not in result.output, result.output
    checked = runner.invoke(app, ["validate", str(target)])
    assert checked.exit_code == 0, checked.output


def test_new_feeder_and_nem2_leave_todos_for_what_they_cant_know(tmp_path):
    feeder = tmp_path / "feeder.yaml"
    result = runner.invoke(app, ["new", "feeder-from-files", str(feeder),
                                 "--premises", str(DATA / "spec_sample/premises.csv"),
                                 "--network", str(DATA / "spec_sample/network.csv"),
                                 "--timeseries", str(DATA / "spec_sample/timeseries.csv"),
                                 "--export-rates", str(DATA / "pge_nbt_eec_2026.csv")])
    assert result.exit_code == 0 and "1 TODO" in result.output
    config = yaml.safe_load(feeder.read_text(encoding="utf-8"))
    assert config["time_grid"]["horizon_steps"] == 96  # the time series file's span
    assert config["feeder"]["value_streams"][0]["element"] == "lateral"  # the network's root
    feeder.write_text(feeder.read_text(encoding="utf-8").replace("value_per_kw: TODO", "value_per_kw: 40"),
                      encoding="utf-8")
    assert runner.invoke(app, ["validate", str(feeder)]).exit_code == 0

    nem2 = tmp_path / "nem2.yaml"
    runner.invoke(app, ["new", "home-bill", str(nem2), "--program", "nem2",
                        "--timeseries", str(DATA / "spec_sample/annual_home.csv"),
                        "--tariff", str(DATA / "tariffs/pge_e_tou_c_2026-06.yaml")])
    assert "baseline_territory: TODO" in nem2.read_text(encoding="utf-8")
    nem2.write_text(nem2.read_text(encoding="utf-8").replace("baseline_territory: TODO", "baseline_territory: X"),
                    encoding="utf-8")
    assert runner.invoke(app, ["validate", str(nem2)]).exit_code == 0
