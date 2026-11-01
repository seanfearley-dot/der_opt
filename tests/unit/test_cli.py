from pathlib import Path

from typer.testing import CliRunner

from der_opt.cli.main import app

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"

runner = CliRunner()


def test_run_prints_status_and_objective():
    result = runner.invoke(app, ["run", str(FIXTURE_PATH)])
    assert result.exit_code == 0
    assert "Status: TerminationCondition.optimal" in result.stdout
    assert "Objective:" in result.stdout
    assert "Value breakdown:" in result.stdout


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
