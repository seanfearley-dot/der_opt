from pathlib import Path

import pytest

from der_opt.devices import BatteryStorage
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.scenarios import load_scenario

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "scenario_basic.yaml"
LBNL_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "lbnl_sample"


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


def test_lbnl_source_external_load_and_daily_tou_and_nbt_export(tmp_path):
    nbt_csv = Path(__file__).parent.parent.parent / "examples" / "data" / "pge_nbt_eec_2026.csv"
    scenario_yaml = f"""
name: lbnl_test_house
time_grid:
  resolution_minutes: 60
  horizon_steps: 4
  start: "2026-01-05T00:00:00"
devices: []
value_streams:
  - type: energy_arbitrage
    params:
      import_price:
        type: daily_tou
        hourly_pattern: [{", ".join(["0.1"] * 24)}]
      export_price:
        type: nbt_export
        csv_path: {nbt_csv}
external_load:
  source: lbnl_net_load
  scenario_dir: {LBNL_FIXTURE_DIR}
  cluster_name: res-test-sin_fam-small
"""
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(scenario_yaml)

    premise = load_scenario(scenario_path)
    assert premise.external_load.tolist() == pytest.approx([10.0, 10.0, 12.0, 7.0])
    dispatch, model, backend = premise.solve()
    assert dispatch.solver_status == "TerminationCondition.optimal"


def test_charge_sources_resolved_by_device_name(tmp_path):
    scenario_yaml = """
name: charge_sources_test
time_grid:
  resolution_minutes: 60
  horizon_steps: 2
devices:
  - type: solar
    name: solar
    params:
      available_kw: [4.0, 0.0]
  - type: battery
    name: battery
    params:
      capacity_kwh: 10
      p_max_charge_kw: 5
      p_max_discharge_kw: 5
      charge_sources: [solar]
external_load: [0.0, 0.0]
"""
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(scenario_yaml)

    premise = load_scenario(scenario_path)
    battery = next(d for d in premise.devices if d.name == "battery")
    assert isinstance(battery, BatteryStorage)
    assert len(battery.charge_sources) == 1
    assert battery.charge_sources[0].name == "solar"


def test_charge_sources_referencing_a_later_device_raises(tmp_path):
    # "solar" is listed *after* "battery", which references it -- must fail clearly
    # rather than silently resolving to nothing.
    scenario_yaml = """
name: bad_order_test
time_grid:
  resolution_minutes: 60
  horizon_steps: 2
devices:
  - type: battery
    name: battery
    params:
      capacity_kwh: 10
      p_max_charge_kw: 5
      p_max_discharge_kw: 5
      charge_sources: [solar]
  - type: solar
    name: solar
    params:
      available_kw: [4.0, 0.0]
external_load: [0.0, 0.0]
"""
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(scenario_yaml)

    with pytest.raises(ValueError, match="solar"):
        load_scenario(scenario_path)


def test_exclusive_import_export_flag_is_threaded_through(tmp_path):
    scenario_yaml = """
name: exclusive_test
time_grid:
  resolution_minutes: 60
  horizon_steps: 2
devices: []
external_load: [1.0, 1.0]
poi_limit_kw: 10
exclusive_import_export: true
"""
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(scenario_yaml)

    premise = load_scenario(scenario_path)
    assert premise.exclusive_import_export is True
