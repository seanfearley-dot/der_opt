from pathlib import Path

import pytest
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


REPO = Path(__file__).parent.parent.parent
BILLING_SECTION = f"""
billing:
  tariff: {REPO / "examples/data/tariffs/pge_e_elec_2026-06.yaml"}
  program:
    type: nbt
    export_rates_csv: {REPO / "examples/data/pge_nbt_eec_2026.csv"}
    acc_plus_per_kwh: 0.0088
"""


def test_billing_section_prints_bills_for_both_runs(tmp_path):
    scenario = tmp_path / "billed.yaml"
    scenario.write_text(
        FIXTURE_PATH.read_text()
        + "\ncounterfactual:\n  policies:\n    battery: {type: self_consumption}\n"
        + BILLING_SECTION
    )
    output_dir = tmp_path / "out"
    result = runner.invoke(app, ["run", str(scenario), "-o", str(output_dir)])
    assert result.exit_code == 0, result.stdout
    assert "Bill (PG&E E-ELEC (bundled), NBT):" in result.stdout
    assert "optimal: billed $" in result.stdout
    assert "counterfactual: billed $" in result.stdout
    assert "Bill savings from optimization: $" in result.stdout
    assert "No true-up" in result.stdout  # 4 hours of data, not a 12-cycle year
    assert (output_dir / "bill_cycles.csv").exists()
    assert (output_dir / "counterfactual_bill_cycles.csv").exists()


def test_billing_unknown_program_type_raises(tmp_path):
    scenario = tmp_path / "bad_program.yaml"
    scenario.write_text(FIXTURE_PATH.read_text() + BILLING_SECTION.replace("type: nbt", "type: nem3"))
    result = runner.invoke(app, ["run", str(scenario)])
    assert result.exit_code != 0
    assert "expected 'nbt' or 'nem2'" in str(result.exception)


def _bill_objective_scenario(tmp_path, billing: str, keep_arbitrage: bool = False) -> Path:
    text = FIXTURE_PATH.read_text()
    if not keep_arbitrage:
        start, end = text.index("value_streams:"), text.index("external_load:")
        text = text[:start] + "value_streams:\n  - type: nbt_bill\n" + text[end:]
    else:
        text = text.replace("value_streams:\n", "value_streams:\n  - type: nbt_bill\n")
    path = tmp_path / "bill_objective.yaml"
    path.write_text(text + "\ncounterfactual:\n  policies:\n    battery: {type: self_consumption}\n" + billing)
    return path


def test_nbt_bill_objective_matches_the_printed_bill(tmp_path):
    result = runner.invoke(app, ["run", str(_bill_objective_scenario(tmp_path, BILLING_SECTION))])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    objective_bill = float(next(l for l in lines if l.strip().startswith("NBTBill:")).split("$")[1])
    billed = float(next(l for l in lines if l.strip().startswith("optimal: billed")).split("$")[1])
    assert objective_bill == pytest.approx(billed, abs=0.005)


def test_nbt_bill_needs_a_billing_section(tmp_path):
    result = runner.invoke(app, ["run", str(_bill_objective_scenario(tmp_path, ""))])
    assert result.exit_code != 0
    assert "needs a billing section" in str(result.exception)


def test_nbt_bill_and_energy_arbitrage_together_are_rejected(tmp_path):
    result = runner.invoke(app, ["run", str(_bill_objective_scenario(tmp_path, BILLING_SECTION, keep_arbitrage=True))])
    assert result.exit_code != 0
    assert "counted twice" in str(result.exception)
