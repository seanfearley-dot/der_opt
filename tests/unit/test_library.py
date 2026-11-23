"""The starter library (examples/library/): every block is used by an example or a
scenario here, the examples validate, and `der-opt new` reuses the blocks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyomo.environ as pyo
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.scenarios import load_scenario, load_scenario_config
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.validate import validate_scenario

REPO = Path(__file__).parent.parent.parent
LIBRARY = REPO / "examples" / "library"
EXAMPLES = REPO / "examples" / "scenarios"
DATA = REPO / "examples" / "data"
HOME = DATA / "spec_sample" / "annual_home.csv"
runner = CliRunner()

# scenarios built here, for the blocks no example uses
LIB = f"\"{LIBRARY.as_posix()}"  # extends by absolute path, from a temporary folder
JULY = f"{LIB}/time/2026-07-13T12_72x60min.yaml\""
LOAD = f"external_load: {{source: timeseries, path: {HOME.as_posix()}, premise_id: home_a, column: load_kw}}\n"
SOLAR = (f"  - {{type: solar, name: solar, params: {{available_kw: {{source: timeseries, path: {HOME.as_posix()}, "
         "premise_id: home_a, column: pv_kw}}}\n")
BUILT_HERE = {
    "thermostat": f"""extends: [{JULY}, {LIB}/devices/thermostat_cooling.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import.yaml", {LIB}/meter/panel_20kw.yaml"]
name: ac
devices:
  - {{name: thermostat, params: {{ambient_temp_c: 35}}}}
""",
    "flexible": f"""extends: [{JULY}, {LIB}/devices/curtailable_load.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import.yaml", {LIB}/meter/panel_20kw.yaml"]
name: flexible
devices:
  - {{name: flexible_load, params: {{baseline_load_kw: 1.5}}}}
""",
    "charge_only": f"""extends: [{JULY}, {LIB}/devices/ev_commuter_charge_only.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import_demand_charge.yaml", {LIB}/meter/panel_20kw.yaml"]
name: charge_only
{LOAD}""",
    "tou_timer": f"""extends: [{JULY}, {LIB}/devices/ev_commuter_charge_only.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import.yaml", {LIB}/meter/panel_20kw.yaml",
          {LIB}/counterfactual/ev_tou_timer.yaml"]
name: tou_timer
{LOAD}""",
    "non_export": f"""extends: [{JULY}, {LIB}/devices/battery_13kwh.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import.yaml", {LIB}/meter/panel_20kw.yaml",
          {LIB}/meter/non_export.yaml", {LIB}/avoided_costs/synthetic_2026.yaml"]
name: non_export
devices:
{SOLAR}{LOAD}""",
    "commitment_tests": f"""extends: [{LIB}/time/year_2026.yaml", {LIB}/devices/battery_13kwh.yaml",
          {LIB}/value_streams/pge_e_elec_2026-06_import.yaml", {LIB}/meter/panel_20kw.yaml",
          {LIB}/programs/summer_peak_commitment.yaml", {LIB}/programs/monthly_test_events.yaml"]
name: commitment_tests
devices:
{SOLAR}{LOAD}""",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("library_scenarios")
    paths = {}
    for name, text in BUILT_HERE.items():
        paths[name] = folder / f"{name}.yaml"
        paths[name].write_text(text, encoding="utf-8")
    return paths


def test_every_block_is_used(built):
    used = set()
    for scenario in [*EXAMPLES.glob("*.yaml"), *built.values()]:
        used |= {p.resolve() for p in compose_scenario(scenario).fragments}
    blocks = {p.resolve() for p in LIBRARY.rglob("*.yaml")}
    assert blocks - used == set(), "blocks no example or test uses"


def test_examples_built_from_blocks_validate():
    external = ("lbnl", "v2x")  # need data outside this repo
    for scenario in sorted(EXAMPLES.glob("*.yaml")):
        assert compose_scenario(scenario).fragments, f"{scenario.name} doesn't use the library"
        if any(word in scenario.name for word in external):
            continue
        if scenario.name.startswith("feeder_bank"):
            pytest.importorskip("xarray")
        report = validate_scenario(scenario).report
        assert not report.errors, f"{scenario.name}: {report.errors}"


def test_thermostat_block_takes_the_scenarios_temperature(built):
    premise = load_scenario(built["thermostat"])
    premise.solve()
    ac = premise.devices[0]
    temps = np.array([pyo.value(ac.temp[t]) for t in premise.time_grid.periods])
    assert (temps >= 20 - 1e-6).all() and (temps <= 25 + 1e-6).all() and (ac.T_amb == 35).all()


def test_a_block_todo_left_unset_names_the_block(tmp_path, built):
    text = built["thermostat"].read_text(encoding="utf-8")
    bare = built["thermostat"].with_name("bare.yaml")
    bare.write_text(text.split("devices:")[0], encoding="utf-8")
    try:
        errors = [f.message for f in validate_scenario(bare).report.errors]
    finally:
        bare.unlink()
    expected = ("devices[0].params.ambient_temp_c is still a placeholder ('TODO: set in the scenario') in "
                "thermostat_cooling.yaml; fill it in")
    assert errors == [expected]


def test_flexible_load_charge_only_ev_and_non_export_blocks_solve(built):
    flexible = load_scenario(built["flexible"])
    dispatch, _, _ = flexible.solve()
    assert "optimal" in str(dispatch.solver_status)
    ev_premise = load_scenario(built["charge_only"])
    ev = ev_premise.devices[0]
    assert ev.p_max_discharge_kw == 0 and ev.degradation_cost_per_kwh == 0 and len(ev.departure_periods) == 3
    assert {type(s).__name__ for s in ev_premise.value_streams} == {"EnergyArbitrage", "DemandCharge"}
    dispatch, _, _ = ev_premise.solve()
    assert "optimal" in str(dispatch.solver_status)
    home = load_scenario(built["non_export"])
    dispatch, _, _ = home.solve()
    assert dispatch.grid_export.max() <= 1e-6
    assert load_scenario_config(built["non_export"]).avoided_costs is not None


def test_program_blocks_combine(built):
    from der_opt.dr.study import _calendars

    config = load_scenario_config(built["commitment_tests"])
    assert config.dr.program.response == "commitment" and config.dr.program.committed_kw == 3.0
    assert config.dr.program.season == list(range(1, 13)) and config.dr.events.random is None
    (events,), _ = _calendars(config, load_scenario(built["commitment_tests"]).time_grid)
    assert [e.day.month for e in events] == list(range(1, 13))
    assert all(e.day.weekday() == 1 and e.day.day <= 7 and e.event_type == "test" for e in events)
    assert not validate_scenario(built["commitment_tests"]).report.errors


@pytest.mark.parametrize("template, args", [
    ("dr-battery", ["--timeseries", "spec_sample/annual_home.csv", "--tariff", "tariffs/pge_e_elec_2026-06.yaml",
                    "--avoided-costs", "avoided_costs/synthetic_2026"]),
    ("home-bill", ["--timeseries", "spec_sample/annual_home.csv", "--tariff", "tariffs/pge_e_tou_c_2026-06.yaml",
                   "--program", "nem2", "--baseline-territory", "X"]),
    ("home-bill", ["--timeseries", "spec_sample/annual_home.csv", "--tariff", "tariffs/pge_e_elec_2026-06.yaml",
                   "--export-rates", "pge_nbt_eec_2026.csv"]),
    ("lmp-battery", ["--price", "caiso_lmp_np15_dam_2026-07-13.csv"]),
])
def test_new_reuses_the_library_and_writes_nothing_into_it(tmp_path, template, args):
    args = [str(DATA / a) if not a.startswith("--") and a not in ("nem2", "X") else a for a in args]
    before = set(LIBRARY.rglob("*"))
    target = tmp_path / "scenario.yaml"
    try:
        result = runner.invoke(app, ["new", template, str(target), *args, "--library", str(LIBRARY)])
    finally:
        added = set(LIBRARY.rglob("*")) - before
        for path in sorted(added, reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
    assert result.exit_code == 0, result.output
    assert not added, f"new wrote into the library: {sorted(added)}"
    assert "new fragment" not in result.output
    if template == "dr-battery":
        assert "3 of the fragments it uses have values marked illustrative" in result.output
    checked = runner.invoke(app, ["validate", str(target)])
    assert checked.exit_code == 0, checked.output
