from pathlib import Path

import pytest

from der_opt.premise import EnergyArbitrage, Premise
from der_opt.scenarios import load_scenario

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"


def test_load_scenario_builds_a_premise():
    premise = load_scenario(FIXTURE_PATH)
    assert isinstance(premise, Premise)
    assert premise.name == "test_house"
    assert {d.name for d in premise.devices} == {"battery", "solar"}
    assert len(premise.value_streams) == 1
    assert isinstance(premise.value_streams[0], EnergyArbitrage)
    assert premise.poi_limit_kw == 20
    assert premise.external_load.tolist() == [1.0, 1.0, 3.0, 3.0]


def test_loaded_scenario_solves():
    premise = load_scenario(FIXTURE_PATH)
    dispatch, model, backend = premise.solve()
    assert dispatch.solver_status == "TerminationCondition.optimal"


def test_unknown_device_type_raises():

    from der_opt.scenarios.loader import load_scenario as _load_scenario

    bad_yaml = FIXTURE_PATH.read_text().replace("type: battery", "type: nonexistent_device")
    bad_path = FIXTURE_PATH.parent / "_bad_scenario_tmp.yaml"
    bad_path.write_text(bad_yaml)
    try:
        with pytest.raises(Exception):
            _load_scenario(bad_path)
    finally:
        bad_path.unlink()


def test_csv_external_load_is_resampled(tmp_path):
    import pandas as pd

    csv_path = tmp_path / "load.csv"
    pd.DataFrame(
        {"timestamp": pd.date_range("2026-06-01", periods=4, freq="h"), "load_kw": [1.5, 2.5, 3.5, 4.5]}
    ).to_csv(csv_path, index=False)

    scenario_yaml = FIXTURE_PATH.read_text().replace(
        "external_load: [1, 1, 3, 3]",
        f"external_load:\n  type: csv\n  path: {csv_path}\n  column: load_kw",
    )
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(scenario_yaml)

    premise = load_scenario(scenario_path)
    assert premise.external_load.tolist() == pytest.approx([1.5, 2.5, 3.5, 4.5])
