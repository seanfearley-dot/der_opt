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
    nbt_csv = Path(__file__).parent.parent.parent / "examples" / "data" / "export_rates/pge_nbt_eec_2026.csv"
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


def test_charge_sources_can_reference_a_later_device(tmp_path):
    # "solar" is listed after the battery that charges from it, as when a battery
    # fragment comes before the scenario's own solar: devices build in dependency
    # order, and keep their listed order in the premise.
    scenario_yaml = """
name: order_test
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
    scenario_path.write_text(scenario_yaml, encoding="utf-8")
    premise = load_scenario(scenario_path)
    assert [d.name for d in premise.devices] == ["battery", "solar"]
    assert premise.devices[0].charge_sources[0] is premise.devices[1]


def test_charge_sources_naming_a_missing_device_raises(tmp_path):
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text("""
name: missing_source
time_grid: {resolution_minutes: 60, horizon_steps: 2}
devices:
  - {type: battery, name: battery, params: {capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5,
                                             charge_sources: [solar]}}
external_load: [0.0, 0.0]
""", encoding="utf-8")
    with pytest.raises(ValueError, match="charges from \\['solar'\\], which aren't devices"):
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


def _scenario_with_policies(tmp_path, policies_yaml: str) -> Path:
    path = tmp_path / "scenario_cf.yaml"
    path.write_text(FIXTURE_PATH.read_text() + "\ncounterfactual:\n  policies:\n" + policies_yaml)
    return path


def test_counterfactual_policies_default_power_limits_to_the_device(tmp_path):
    from der_opt.scenarios import (
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )
    from der_opt.simulation import SelfConsumptionBatteryPolicy

    path = _scenario_with_policies(tmp_path, "    battery: {type: self_consumption}\n")
    config = load_scenario_config(path)
    premise = build_premise(config)
    policies = build_counterfactual_policies(config, premise)

    assert isinstance(policies["battery"], SelfConsumptionBatteryPolicy)
    assert policies["battery"].p_max_charge_kw == 5
    assert policies["battery"].p_max_discharge_kw == 5


def test_counterfactual_policy_overrides(tmp_path):
    from der_opt.scenarios import (
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )

    path = _scenario_with_policies(tmp_path, "    battery: {type: self_consumption, p_max_charge_kw: 2.5}\n")
    config = load_scenario_config(path)
    policies = build_counterfactual_policies(config, build_premise(config))
    assert policies["battery"].p_max_charge_kw == 2.5
    assert policies["battery"].p_max_discharge_kw == 5


def test_no_counterfactual_section_gives_none():
    from der_opt.scenarios import (
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )

    config = load_scenario_config(FIXTURE_PATH)
    assert build_counterfactual_policies(config, build_premise(config)) is None


def test_counterfactual_policy_for_unknown_device_raises(tmp_path):
    from der_opt.scenarios import load_scenario_config

    path = _scenario_with_policies(tmp_path, "    not_a_device: {type: self_consumption}\n")
    # caught when the scenario is resolved, before anything is built
    with pytest.raises(ValueError, match="unknown device 'not_a_device'"):
        load_scenario_config(path)


def test_counterfactual_policy_on_wrong_device_type_raises(tmp_path):
    # rule_based_ev needs soc_max; solar has no such attribute -- should say so
    # clearly rather than failing deep inside the simulator.
    from der_opt.scenarios import (
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )

    path = _scenario_with_policies(tmp_path, "    solar: {type: rule_based_ev}\n")
    config = load_scenario_config(path)
    with pytest.raises(ValueError, match="wrong policy for this device type"):
        build_counterfactual_policies(config, build_premise(config))


def test_retail_tariff_and_nbt_adder_price_types(tmp_path):
    repo = Path(__file__).parent.parent.parent
    scenario_yaml = f"""
name: tariff_price_test
time_grid:
  resolution_minutes: 60
  horizon_steps: 24
  start: "2026-07-15T00:00:00"
devices: []
value_streams:
  - type: energy_arbitrage
    params:
      import_price:
        type: retail_tariff
        path: {repo / "examples/data/tariffs/pge_e_elec_2026-06.yaml"}
      export_price:
        type: nbt_export
        csv_path: {repo / "examples/data/export_rates/pge_nbt_eec_2026.csv"}
        adder_per_kwh: 0.0088
external_load: [{", ".join(["1.0"] * 24)}]
"""
    path = tmp_path / "scenario.yaml"
    path.write_text(scenario_yaml)
    premise = load_scenario(path)
    stream = premise.value_streams[0]
    assert stream._import_rates[18] == pytest.approx(0.55214)  # E-ELEC summer peak
    from der_opt.integrations.pge_nbt_export import NBTExportPriceSeries

    without_adder = NBTExportPriceSeries(repo / "examples/data/export_rates/pge_nbt_eec_2026.csv").values(premise.time_grid)
    assert stream._export_rates == pytest.approx(without_adder + 0.0088)
