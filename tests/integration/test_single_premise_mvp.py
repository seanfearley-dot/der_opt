"""Golden-file regression test: a full 4-DER, 24h/15-min scenario (battery, solar,
thermostat, curtailable load) under a TOU tariff, solved and compared against a stored
summary of a known-good run. A change to this test's expected JSON should only happen
alongside a deliberate model/scenario change -- regenerate it by running this scenario
and updating tests/integration/golden/expected_results_basic_4der.json."""

from __future__ import annotations

import json
from pathlib import Path

import pyomo.environ as pyo
import pytest

from der_opt.pricing import extract_duals
from tests.integration.golden.scenario_basic_4der import build_premise

GOLDEN_PATH = Path(__file__).parent / "golden" / "expected_results_basic_4der.json"


def _summarize(dispatch, model, backend) -> dict:
    duals = extract_duals(model, backend)
    return {
        "solver_status": dispatch.solver_status,
        "objective_value": dispatch.objective_value,
        "grid_import_sum": float(dispatch.grid_import.sum()),
        "grid_export_sum": float(dispatch.grid_export.sum()),
        "device_column_sums": {
            col: float(dispatch.device_dispatch[col].sum()) for col in dispatch.device_dispatch.columns
        },
        "duals_exact_flags": {name: d.is_exact for name, d in duals.items()},
        "poi_balance_dual_sample": {
            str(idx): float(val) for idx, val in duals["golden_house_poi_balance"].values.items()
            if idx in (0, 40, 80, 95)
        },
    }


@pytest.fixture(scope="module")
def solved_summary():
    premise = build_premise()
    dispatch, model, backend = premise.solve()
    return _summarize(dispatch, model, backend)


def test_golden_scenario_matches_stored_snapshot(solved_summary):
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert solved_summary["solver_status"] == expected["solver_status"]
    assert solved_summary["objective_value"] == pytest.approx(expected["objective_value"], abs=1e-4)
    assert solved_summary["grid_import_sum"] == pytest.approx(expected["grid_import_sum"], abs=1e-4)
    assert solved_summary["grid_export_sum"] == pytest.approx(expected["grid_export_sum"], abs=1e-4)
    assert solved_summary["duals_exact_flags"] == expected["duals_exact_flags"]

    for col, value in expected["device_column_sums"].items():
        # The battery's charge path isn't unique: when holding energy an hour longer
        # costs nothing, equally cheap schedules differ only in when it sits charged,
        # and solver versions pick different ones (HiGHS 1.15 vs 1.16 here). The
        # objective, flows, and duals above pin the solution; the path doesn't.
        if col.endswith(".soc"):
            continue
        assert solved_summary["device_column_sums"][col] == pytest.approx(value, abs=1e-4)

    for idx, value in expected["poi_balance_dual_sample"].items():
        assert solved_summary["poi_balance_dual_sample"][idx] == pytest.approx(value, abs=1e-4)


def test_golden_scenario_satisfies_energy_balance_every_period():
    premise = build_premise()
    dispatch, model, backend = premise.solve()
    for t in premise.time_grid.periods:
        device_power = sum(d.net_power_expr(model, t) for d in premise.devices)
        lhs = pyo.value(premise.grid_import[t]) - pyo.value(premise.grid_export[t]) + pyo.value(device_power)
        assert lhs == pytest.approx(premise.external_load[t], abs=1e-6)
