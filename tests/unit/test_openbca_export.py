"""The OpenBCA export (docs/COST_TEST_SPEC.md v1.1). Unit tests check the tables der-opt
writes. The integration test runs OpenBCA's own core on them and reconciles a stream
against an independent calculation; it needs an OpenBCA checkout with its environment:
OPENBCA_DIR=/path/to/openbca pytest tests/unit/test_openbca_export.py"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.avoided_costs import read_set
from der_opt.cli.main import app
from der_opt.cost_test.openbca import (
    COST_TREATMENT,
    avoided_costs_for_year,
    export_openbca,
    run_openbca,
    tally,
)

REPO = Path(__file__).parent.parent.parent
OPENBCA_DIR = os.environ.get("OPENBCA_DIR")


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    result = CliRunner().invoke(app, ["run", str(REPO / "examples/scenarios/dr_battery_annual.yaml"), "-o", str(out)])
    assert result.exit_code == 0, result.stdout
    return out


@pytest.fixture(scope="module")
def exported(run_dir):
    return export_openbca(run_dir / "measure_impacts", run_dir / "openbca")


def _table(item, name):
    return pd.read_csv(item.folder / "data" / f"{name}.csv")


def test_one_configuration_per_test(exported):
    assert [e.test for e in exported] == ["PCT", "RIM", "TRC", "PAC", "SCT"]
    for item in exported:
        gp = _table(item, "global_parameters").iloc[0]
        assert gp["cost_treatment"] == COST_TREATMENT[item.test]
        assert gp["discount_rate"] == {"PCT": 0.10, "SCT": 0.03}.get(item.test, 0.075)
        assert gp["electric_line_loss"] == 0 and gp["peak_capacity_line_loss"] == 0
        assert (item.folder / "input_project" / "models" / "measures.sql").exists()


def test_measure_rows_and_custom_slots(exported):
    measures = _table(exported[0], "measures")
    assert list(measures["id"]) == ["dr_battery_annual", "dr_battery_annual-increases"]
    categories = measures.filter(like="_value_stream_impact_category").iloc[0]
    assert categories.notna().all() and categories.is_unique  # OpenBCA uses them as map keys
    main, increases = measures.iloc[0], measures.iloc[1]
    assert main["coincident_peak_savings_kw"] == 1 and increases["coincident_peak_savings_kw"] == 0
    assert pd.isna(increases["incremental_cost_upfront_dollar"])  # costs only on the measure row


def test_load_shapes_and_avoided_costs(exported, run_dir):
    shapes = _table(exported[0], "load_shapes_ts")
    energy = pd.read_csv(run_dir / "measure_impacts" / "energy_impact.csv")
    main = shapes[shapes["load_shape"] == "dr_battery_annual"]
    rise = shapes[shapes["load_shape"] == "dr_battery_annual-increases"]
    assert len(main) == len(rise) == 8760 and (main["load_shape_value"] >= 0).all() and (rise["load_shape_value"] <= 0).all()
    assert main["load_shape_value"].sum() * 1000 == pytest.approx(energy["decrease_kwh"].sum(), rel=1e-6)  # MWh
    costs = _table(exported[2], "avoided_costs_ts")  # TRC
    hourly = costs[costs["avoided_cost"] == "energy"]
    assert hourly.groupby("year").size().eq(8760).all() and set(hourly["year"]) == set(range(2027, 2037))
    assert hourly["hour_of_day"].between(1, 24).all() and hourly["hour_of_year"].between(0, 8759).all()


def test_capacity_values_apply_the_monthly_shares(exported, run_dir):
    acs = read_set(REPO / "examples/data/avoided_costs/synthetic_2026")
    kw = pd.read_csv(run_dir / "measure_impacts" / "capacity.csv").set_index("month")["valued_kw"]
    costs = _table(exported[2], "avoided_costs_ts")
    row = costs[(costs["avoided_cost"] == "DR capacity (generation)") & (costs["year"] == 2030)].iloc[0]
    price, shares = acs.capacity_price(2030, "generation")
    assert row["avoided_cost_value"] == pytest.approx(price * (shares * kw.reindex(range(1, 13)).to_numpy()).sum())
    assert row["avoided_cost_subset"] == "dr_battery_annual"


def test_stream_inclusion_by_test(exported):
    included = {item.test: set(_table(item, "value_stream_groups").query("include_in_test")["avoided_cost"])
                for item in exported}
    assert "energy" not in included["PCT"] and "energy" in included["TRC"]
    assert "DR capacity (generation)" in included["PAC"]
    for test in ("TRC", "PAC", "SCT"):
        assert "Participant bill savings" not in included[test]


def test_avoided_cost_calendar_maps_by_date():
    acs = read_set(REPO / "examples/data/avoided_costs/synthetic_2026")
    values = avoided_costs_for_year(acs, 2027, 2026, ["energy"])
    assert len(values) == 8760
    # 2027-07-15 18:00 standard time is 19:00 daylight time, the evening peak
    local = acs.year_local(2027)
    assert values.loc["2026-07-15 18:00", "energy"] == pytest.approx(local.loc["2027-07-15 19:00", "energy"])


def test_openbca_gets_absolute_paths_when_called_with_relative_ones(exported, run_dir, tmp_path, monkeypatch):
    """OpenBCA runs from its own checkout, so relative paths (the default out/<name>, a
    relative --openbca) must be made absolute before it's called."""
    import subprocess

    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (checkout / "core").mkdir()
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: calls.append((command, kw))
                        or subprocess.CompletedProcess(command, 0, "", ""))
    monkeypatch.chdir(tmp_path)
    run_openbca(exported[:1], Path("checkout"), Path(os.path.relpath(run_dir / "openbca" / "run_core.py", tmp_path)))
    (command, kw), = calls
    assert all(Path(arg).is_absolute() for arg in command), command
    assert Path(kw["cwd"]).is_absolute() and Path(command[0]) == (checkout / ".venv" / "bin" / "python").resolve()


@pytest.mark.skipif(not OPENBCA_DIR, reason="needs an OpenBCA checkout: set OPENBCA_DIR")
def test_openbca_runs_and_reconciles(exported, run_dir):
    run_openbca(exported, OPENBCA_DIR, run_dir / "openbca" / "run_core.py")
    table = tally(exported).set_index("test")
    for test in table.index:  # OpenBCA's own figures, as it reports them
        own = pd.read_csv(run_dir / "openbca" / test / "output" / "jst_ratio.csv").iloc[0]
        assert table.loc[test, "openbca_ratio"] == pytest.approx(own["jst_ratio"], nan_ok=True)
        assert table.loc[test, "net_benefits"] == pytest.approx(own["net_benefits"], nan_ok=True)
    # PAC counts 10 x $15 admin and 10 x the DR incentive undiscounted; at 7.5% they'd be
    # worth 7.38/10 as much
    annual = 10 * 15 + 10 * yaml.safe_load((run_dir / "measure_impacts" / "measure.yaml").read_text(
        encoding="utf-8"))["first_year"]["dr_incentive"]
    annuity = sum(1 / 1.075 ** t for t in range(10)) / 10
    assert table.loc["PAC", "net_change_if_annual_discounted"] == pytest.approx(annual * (1 - annuity), rel=1e-4)
    assert table.loc["PCT", "net_change_if_annual_discounted"] < 0  # the incentive is a PCT benefit: overstated
    # OpenBCA's discounted energy stream equals an independent calculation (to float32;
    # $/kWh values would be rounded to 3 decimals by OpenBCA, hence the MWh units)
    acs = read_set(REPO / "examples/data/avoided_costs/synthetic_2026")
    decrease = pd.read_csv(run_dir / "measure_impacts" / "energy_impact.csv")["decrease_kwh"].to_numpy()
    expected = sum(
        (avoided_costs_for_year(acs, y, 2026, ["energy"])["energy"].to_numpy() * decrease).sum() / 1.075 ** (y - 2027)
        for y in range(2027, 2037))
    streams = pd.read_csv(exported[2].folder / "output" / "streams.csv")
    got = streams.query("id == 'dr_battery_annual' and value_stream == 'energy'")["present_value"].sum()
    assert got == pytest.approx(expected, rel=1e-5)
    assert streams.query("value_stream == 'GHG Intensity (E)'")["ghg_savings"].abs().sum() > 0  # not rounded to 0
    measure = yaml.safe_load((run_dir / "measure_impacts" / "measure.yaml").read_text(encoding="utf-8"))
    assert measure["discount_rates"]["utility"] == 0.075 and np.isfinite(table.loc["TRC", "openbca_ratio"])
