from pathlib import Path

from typer.testing import CliRunner

from der_opt.cli.main import app

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"

runner = CliRunner()


def _with_counterfactual(tmp_path, policies_yaml: str) -> Path:
    path = tmp_path / "scenario_cf.yaml"
    path.write_text(FIXTURE_PATH.read_text() + "\ncounterfactual:\n  policies:\n" + policies_yaml)
    return path


def test_run_prints_status_and_objective():
    result = runner.invoke(app, ["run", str(FIXTURE_PATH)])
    assert result.exit_code == 0
    assert "Status: TerminationCondition.optimal" in result.stdout
    assert "Optimal dispatch: $" in result.stdout
    # no counterfactual section in this scenario, so no comparison
    assert "Counterfactual" not in result.stdout


def test_run_with_counterfactual_reports_value_of_optimization(tmp_path):
    scenario = _with_counterfactual(tmp_path, "    battery: {type: self_consumption}\n")
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code == 0, result.stdout
    assert "Counterfactual (battery: self_consumption): $" in result.stdout
    assert "Value of optimization: $" in result.stdout


def test_counterfactual_never_beats_optimal_when_feasible_for_the_optimizer(tmp_path):
    # an idle battery is a feasible dispatch for the optimizer (no terminal SOC floor
    # in this fixture), and both are scored on the same value streams -- so the
    # optimum can't be worse. If this ever goes negative, the two are being scored on
    # different problems.
    scenario = _with_counterfactual(tmp_path, "    battery: {type: no_flexibility}\n")
    result = runner.invoke(app, ["run", str(scenario)])
    line = next(l for l in result.stdout.splitlines() if l.startswith("Value of optimization"))
    assert float(line.split("$")[1]) >= -1e-6


def test_run_with_end_state_mismatch_prints_note(tmp_path):
    # battery starts at 50% and is idle in the counterfactual, so it ends at 5 kWh;
    # the optimizer drains it for revenue -- the comparison should flag that.
    scenario = _with_counterfactual(tmp_path, "    battery: {type: no_flexibility}\n")
    result = runner.invoke(app, ["run", str(scenario)])
    assert "Note: battery ends at" in result.stdout


def test_no_counterfactual_flag_skips_the_comparison(tmp_path):
    scenario = _with_counterfactual(tmp_path, "    battery: {type: self_consumption}\n")
    result = runner.invoke(app, ["run", str(scenario), "--no-counterfactual"])
    assert result.exit_code == 0
    assert "Counterfactual" not in result.stdout


def test_counterfactual_outputs_written(tmp_path):
    scenario = _with_counterfactual(tmp_path, "    battery: {type: self_consumption}\n")
    output_dir = tmp_path / "out"
    result = runner.invoke(app, ["run", str(scenario), "--duals", "-o", str(output_dir)])
    assert result.exit_code == 0, result.stdout
    for name in (
        "counterfactual_device_dispatch.csv", "counterfactual_grid_import.csv",
        "counterfactual_grid_export.csv", "counterfactual_dispatch.png", "comparison.png",
        "value_breakdown.csv", "duals.csv", "duals.png",
    ):
        assert (output_dir / name).exists(), name

    import pandas as pd

    table = pd.read_csv(output_dir / "value_breakdown.csv", index_col="source")
    assert list(table.columns) == ["optimal", "counterfactual"]
    assert "total" in table.index


def test_run_with_duals_flag_prints_dual_families():
    result = runner.invoke(app, ["run", str(FIXTURE_PATH), "--duals"])
    assert result.exit_code == 0
    assert "Dual/shadow-price families" in result.stdout
    assert "test_house_poi_balance" in result.stdout


def test_run_with_output_dir_writes_csvs_and_charts(tmp_path):
    output_dir = tmp_path / "out"
    result = runner.invoke(app, ["run", str(FIXTURE_PATH), "--output-dir", str(output_dir)])
    assert result.exit_code == 0
    assert (output_dir / "device_dispatch.csv").exists()
    assert (output_dir / "grid_import.csv").exists()
    assert (output_dir / "grid_export.csv").exists()
    assert (output_dir / "dispatch.png").exists()
    assert (output_dir / "value_stack.png").exists()


def test_run_requires_subcommand_name():
    # single-command Typer apps collapse into a bare invocation unless a no-op
    # @app.callback() forces subcommand-style dispatch -- this guards that choice.
    result = runner.invoke(app, [str(FIXTURE_PATH)])
    assert result.exit_code != 0
