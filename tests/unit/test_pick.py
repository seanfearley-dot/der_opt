"""Picking library blocks: `der-opt blocks`, `der-opt new --pick` and `--use`, and
`der-opt setup library` (docs/SCENARIO_COMPOSITION_SPEC.md section 3a)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.library import STARTER_LIBRARY, read_library

REPO = Path(__file__).parent.parent.parent
HOME = REPO / "examples" / "data" / "spec_sample" / "annual_home.csv"
runner = CliRunner()
DR_BLOCKS = ["year_2026", "battery_13kwh", "pge_e_elec_2026-06_import", "panel_20kw", "battery_self_consumption",
             "summer_peak_incentive", "cost_tests/synthetic_2026"]


@pytest.fixture(autouse=True)
def private_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DER_OPT_CONFIG", str(tmp_path / "config" / "config.yaml"))


def _use(target: Path, names: list[str], *extra: str):
    return runner.invoke(app, ["new", str(target), *[a for n in names for a in ("--use", n)], *extra])


def test_blocks_lists_every_block_by_kind_with_what_it_needs():
    result = runner.invoke(app, ["blocks"])
    assert result.exit_code == 0, result.output
    assert f"{STARTER_LIBRARY} (der-opt's starter library): 33 blocks" in result.output
    assert "devices/thermostat_cooling" in result.output and "needs: ambient_temp_c" in result.output
    assert "DR add-ons (after the program):\n  programs/monthly_test_events" in result.output
    kinds = {b.name: b.kind for b in read_library(STARTER_LIBRARY)}
    assert kinds["programs/summer_peak_commitment"] == "dr" and kinds["study/homes_bill"] == "study"
    assert kinds["meter/no_simultaneous_import_export"] == "meter"


def test_use_rebuilds_the_dr_example(tmp_path):
    target = tmp_path / "home.yaml"
    result = _use(target, DR_BLOCKS, "--timeseries", str(HOME))
    assert result.exit_code == 0, result.output
    assert "hold values marked illustrative" in result.output
    mine = compose_scenario(target).data
    example = compose_scenario(REPO / "examples/scenarios/dr_battery_annual.yaml").data
    for data in (mine, example):
        data.pop("name"), data.pop("weather_case", None)
        data["devices"] = sorted(data["devices"], key=lambda d: d["name"])
    assert mine == example
    assert runner.invoke(app, ["validate", str(target)]).exit_code == 0


def test_per_home_placeholders_go_in_the_scenario(tmp_path):
    target = tmp_path / "ac.yaml"
    result = _use(target, ["year_2026", "thermostat_cooling", "pge_e_elec_2026-06_import", "pge_e_tou_c_2026-06_nem2"],
                  "--timeseries", str(HOME))
    assert result.exit_code == 0 and "2 TODO(s) to fill in, in ac.yaml" in result.output
    own = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert own["devices"][1] == {"name": "thermostat", "params": {"ambient_temp_c": "TODO: set in the scenario"}}
    assert own["billing"] == {"baseline_territory": "TODO: set in the scenario"}
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("ambient_temp_c: 'TODO: set in the scenario'", "ambient_temp_c: 30").replace(
        "baseline_territory: 'TODO: set in the scenario'", "baseline_territory: X"), encoding="utf-8")
    assert runner.invoke(app, ["validate", str(target)]).exit_code == 0


@pytest.mark.parametrize("names, problem", [
    (["battery_13kwh", "pge_e_elec_2026-06_import"], "pick a time grid block"),
    (["year_2026", "battery_13kwh"], "pick a prices and objective block"),
    (["year_2026", "bill", "caiso_lmp_np15_dam_2026-07-13"], "pick one prices and objective block, not 2"),
    (["year_2026", "bill"], "the bill objective needs a billing block"),
    (["year_2026", "battery_13kwh", "battery_1mw_4mwh", "bill"], "both have a device named 'battery'"),
    (["year_2026", "bill", "ev_plug_and_charge"], "sets a counterfactual for 'ev', which isn't one of your devices"),
    (["year_2026", "bill", "panel_15kw", "panel_20kw"], "both set poi_limit_kw (20 vs 15.0)"),
    (["year_2026", "bill", "monthly_test_events"], "adds to a DR program; pick a program too"),
    (["year_2026", "pge_e_elec_2026-06_import", "summer_peak_incentive"], "counts ['battery'] as the measure"),
    (["year_2026", "bill", "homes_bill"], "is for studies"),
])
def test_picks_that_dont_fit_are_refused(tmp_path, names, problem):
    target = tmp_path / "bad.yaml"
    result = _use(target, names)
    assert result.exit_code == 1 and problem in result.output, result.output
    assert "nothing was written" in result.output and not target.exists()
    assert "Change the picks and run it again" in result.output


def test_names_must_be_unique_or_have_their_folder(tmp_path):
    result = _use(tmp_path / "x.yaml", ["synthetic_2026"])
    assert result.exit_code != 0
    flat = " ".join(result.output.replace("│", " ").split())
    assert "avoided_costs/synthetic_2026, cost_tests/synthetic_2026" in flat
    assert "no block 'nope'" in " ".join(_use(tmp_path / "x.yaml", ["nope"]).output.replace("│", " ").split())


def test_pick_is_retired_for_the_app(tmp_path):
    result = runner.invoke(app, ["new", str(tmp_path / "menu.yaml"), "--pick"])
    assert result.exit_code != 0 and "der-opt ui" in " ".join(result.output.replace("│", " ").split())
    assert not (tmp_path / "menu.yaml").exists()


def test_setup_library_is_remembered_and_used(tmp_path):
    mine = tmp_path / "my_library" / "time"
    mine.mkdir(parents=True)
    (mine / "short.yaml").write_text("# a short run\ntime_grid: {resolution_minutes: 60, horizon_steps: 24, "
                                     'start: "2026-07-13T00:00:00"}\n', encoding="utf-8")
    assert runner.invoke(app, ["setup", "library", str(tmp_path / "nowhere")]).exit_code != 0
    result = runner.invoke(app, ["setup", "library", str(tmp_path / "my_library")])
    assert result.exit_code == 0 and "(1 blocks)" in result.output
    listed = runner.invoke(app, ["blocks"])
    assert "my_library" in listed.output and "time/short" in listed.output and "a short run" in listed.output
    assert "year_2026" not in listed.output
    starter = runner.invoke(app, ["blocks", "--library", str(STARTER_LIBRARY)])
    assert "year_2026" in starter.output
