"""The post-solve checks, the CSV gap warnings, and the run record."""

from __future__ import annotations

import dataclasses
import json
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.diagnostics import (
    CheckReport,
    check_dispatch,
    check_end_states,
    check_prices,
    check_tariff_dates,
    collect_data_warnings,
)
from der_opt.premise import EnergyArbitrage
from der_opt.pricing import ConstantPriceSeries, CSVPriceSeries, DataGapWarning
from der_opt.scenarios import build_premise, load_scenario_config
from der_opt.time_grid import TimeGrid

FIXTURE = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"


@pytest.fixture
def solved():
    premise = build_premise(load_scenario_config(FIXTURE))
    dispatch, _, _ = premise.solve()
    return premise, dispatch


def _checks(report: CheckReport) -> set[str]:
    return {f.check for f in report.findings}


def test_a_clean_solve_has_no_findings(solved):
    premise, dispatch = solved
    report = check_dispatch(premise, dispatch)
    assert report.findings == []
    assert {"energy balance", "storage limits", "objective matches re-pricing"} <= set(report.checks_run)


def test_energy_that_doesnt_balance_is_an_error(solved):
    premise, dispatch = solved
    broken = dataclasses.replace(dispatch, grid_import=dispatch.grid_import + 0.5)
    report = check_dispatch(premise, broken)
    assert "energy balance" in _checks(report) and report.errors


def test_objective_that_disagrees_with_repricing_is_an_error(solved):
    # the class of bug where a rate misses its x dt
    premise, dispatch = solved
    report = check_dispatch(premise, dataclasses.replace(dispatch, objective_value=dispatch.objective_value * 4 + 1))
    assert "objective matches re-pricing" in _checks(report)


def test_storage_outside_its_limits_is_an_error(solved):
    premise, dispatch = solved
    frame = dispatch.device_dispatch.copy()
    frame["battery.soc"] = -1.0
    assert "storage limits" in _checks(check_dispatch(premise, dataclasses.replace(dispatch, device_dispatch=frame)))


def test_simultaneous_flows_are_warnings(solved):
    premise, dispatch = solved
    frame = dispatch.device_dispatch.copy()
    frame["battery.p_ch"], frame["battery.p_dis"] = 1.0, 1.0
    both = dataclasses.replace(dispatch, device_dispatch=frame,
                               grid_import=dispatch.grid_import + 2, grid_export=dispatch.grid_export + 2)
    report = check_dispatch(premise, both)
    assert {"charge and discharge at once", "import and export at once"} <= _checks(report)
    assert all(f.level == "warning" for f in report.findings if f.check != "objective matches re-pricing")


def test_end_states_that_differ_are_flagged(solved):
    premise, dispatch = solved
    emptier = dataclasses.replace(dispatch, final_state={"battery": {"soc": 0.0}})
    fuller = dataclasses.replace(dispatch, final_state={"battery": {"soc": 8.0}})
    report = check_end_states(premise, {"optimal": emptier, "counterfactual": fuller})
    assert "favors the optimal run" in report.findings[0].message
    # a lone run that drains below its start is flagged too
    assert check_end_states(premise, {"optimal": emptier}).findings


def test_prices_in_the_wrong_units_are_flagged(solved):
    premise, _ = solved
    premise.value_streams = [EnergyArbitrage(ConstantPriceSeries(45.0), ConstantPriceSeries(0.0), premise.time_grid)]
    assert "$/MWh" in check_prices(premise).findings[0].message


def test_tariff_dates():
    class Tariff:
        name, effective_date = "Test rate", "2026-06-01"

    early = TimeGrid(60, 24, datetime(2026, 1, 1))
    current = TimeGrid(60, 24, datetime(2026, 7, 1))
    stale = TimeGrid(60, 24, datetime(2027, 8, 1))
    assert "after this run starts" in check_tariff_dates(Tariff, early).findings[0].message
    assert check_tariff_dates(Tariff, current).findings == []
    assert "newer rates" in check_tariff_dates(Tariff, stale).findings[0].message


# --- CSV inputs ---------------------------------------------------------------------

GRID = TimeGrid(60, 48, datetime(2026, 7, 1))


def test_gaps_in_csv_data_warn_and_repeat_the_last_value():
    index = pd.date_range("2026-07-01", periods=48, freq="h")
    series = pd.Series(np.arange(48.0), index=index).drop(index[10:20])
    with pytest.warns(DataGapWarning, match="10 of 48 steps"):
        values = CSVPriceSeries(series, label="load.csv [kw]").values(GRID)
    assert values[10:20].tolist() == [9.0] * 10


def test_finer_csv_data_is_averaged_not_sampled():
    index = pd.date_range("2026-07-01", periods=48 * 4, freq="15min")
    series = pd.Series(np.tile([0.0, 0.0, 0.0, 4.0], 48), index=index)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DataGapWarning)
        values = CSVPriceSeries(series).values(GRID)
    assert values.tolist() == [1.0] * 48  # the top-of-hour sample alone would say 0


def test_coarser_csv_data_is_held_without_a_warning():
    index = pd.date_range("2026-07-01", periods=2, freq="D")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DataGapWarning)
        values = CSVPriceSeries(pd.Series([1.0, 2.0], index=index)).values(GRID)
    assert values.tolist() == [1.0] * 24 + [2.0] * 24


def test_duplicates_warn_and_zoned_timestamps_are_refused():
    index = pd.date_range("2026-07-01", periods=48, freq="h")
    doubled = pd.concat([pd.Series(1.0, index=index), pd.Series(3.0, index=index[:1])])
    with pytest.warns(DataGapWarning, match="1 duplicate"):
        assert CSVPriceSeries(doubled).values(GRID)[0] == 2.0
    with pytest.raises(ValueError, match="time zone"):
        CSVPriceSeries(pd.Series(1.0, index=index.tz_localize("UTC"))).values(GRID)


def test_collected_data_warnings_become_findings():
    report = CheckReport()
    with collect_data_warnings(report):
        warnings.warn(DataGapWarning("load.csv: 3 steps have no data"))
    assert report.findings[0].check == "input data" and "3 steps" in report.findings[0].message


# --- the CLI ------------------------------------------------------------------------


def test_run_prints_checks_and_writes_a_run_record(tmp_path):
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["run", str(FIXTURE), "-o", str(out)])
    assert result.exit_code == 0, result.stdout
    assert "Checks: " in result.stdout and "0 errors" in result.stdout
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert record["scenario"]["sha256"] and record["environment"]["packages"]["pyomo"]
    assert record["code"]["git_commit"]  # this repo is a git checkout
    assert record["checks"]["summary"].endswith("0 errors")
    assert (out / "scenario.yaml").read_bytes() == FIXTURE.read_bytes()
    assert (out / "checks.csv").exists()


def test_run_record_fingerprints_input_files(tmp_path):
    prices = tmp_path / "prices.csv"
    pd.DataFrame({"timestamp": pd.date_range("2026-06-01", periods=4, freq="h"), "p": 0.2}).to_csv(prices, index=False)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(FIXTURE.read_text(encoding="utf-8").replace(
        "import_price: {type: constant, price: 0.2}", f"import_price: {{type: csv, path: {prices}, column: p}}"
    ), encoding="utf-8")
    out = tmp_path / "out"
    assert CliRunner().invoke(app, ["run", str(scenario), "-o", str(out)]).exit_code == 0
    inputs = json.loads((out / "run_record.json").read_text(encoding="utf-8"))["inputs"]
    assert [i["path"] for i in inputs] == [str(prices)] and len(inputs[0]["sha256"]) == 64
