"""The population model, phase 1 (docs/POPULATION_MODEL_SPEC.md): the precedence rule,
device templates, and every form of premise resolving to one model with each value's
source."""

from __future__ import annotations

from pathlib import Path

import pytest

from der_opt.population.compile import resolve_feeder, resolve_scenario
from der_opt.population.precedence import Layer, resolve
from der_opt.population.templates import load_template
from der_opt.scenarios import (
    build_feeder_scenario,
    build_premise,
    load_feeder_config,
    load_scenario_config,
)
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.schema import FeederScenarioConfig, ScenarioConfig

REPO = Path(__file__).parent.parent.parent
EXAMPLES = sorted((REPO / "examples" / "scenarios").glob("*.yaml"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- precedence -----------------------------------------------------------------------


def test_layers_merge_weakest_first_whatever_order_they_come_in():
    values, sources = resolve([
        Layer("row", "row 2", {"eta_charge": 0.9, "pattern": {"weekend": "home"}}),
        Layer("built-in", "built-in", {"eta_charge": 0.95, "soc_min_frac": 0.0}),
        Layer("template", "template:t", {"eta_charge": 0.92, "pattern": {"weekday": {"miles": 30}, "weekend": {"miles": 5}}}),
        Layer("defaults", "defaults[0]", {"soc_min_frac": 0.2}),
        Layer("override", "override:h/ev", {"pattern": {"weekday": {"miles": 40}}}),
    ])
    assert values == {"eta_charge": 0.9, "soc_min_frac": 0.2, "pattern": {"weekday": {"miles": 40}, "weekend": "home"}}
    assert sources == {"eta_charge": "row 2", "soc_min_frac": "defaults[0]", "pattern.weekday.miles": "override:h/ev",
                       "pattern.weekend": "row 2"}


def test_data_sources_and_lists_are_replaced_whole_and_null_removes():
    values, sources = resolve([
        Layer("template", "template:t", {"available_kw": {"source": "pvwatts", "lat": 37}, "charge_sources": ["a", "b"],
                                         "soc_final_min_frac": 0.5}),
        Layer("row", "row 3", {"available_kw": {"source": "timeseries", "column": "pv_kw"}, "charge_sources": ["c"],
                               "soc_final_min_frac": None}),
    ])
    assert values == {"available_kw": {"source": "timeseries", "column": "pv_kw"}, "charge_sources": ["c"]}
    assert sources == {"available_kw": "row 3", "charge_sources": "row 3"}
    with pytest.raises(ValueError, match="unknown precedence level"):
        Layer("study", "x", {})


# --- templates ------------------------------------------------------------------------


def test_templates_in_both_forms(tmp_path):
    block = load_template(REPO / "examples/library/devices/battery_13kwh.yaml")
    assert block.name == "battery_13kwh" and block.type == "battery" and block.params["capacity_kwh"] == 13.5
    child = load_template(REPO / "examples/library/devices/ev_commuter_charge_only.yaml")  # a block that extends one
    assert child.type == "ev_v2x" and child.params["p_max_discharge_kw"] == 0 and "pattern" in child.params
    own = load_template(_write(tmp_path / "managed.yaml", """
template: managed_charger
type: ev
params: {capacity_kwh: 60, p_max_charge_kw: 7.2, p_max_discharge_kw: 0, soc_init_frac: 0.8}
counterfactual: {type: rule_based_ev}
rules: {dr_event: {action: pause, catch_up: false}}
"""))
    assert own.name == "managed_charger" and own.type == "ev_v2x" and own.counterfactual == {"type": "rule_based_ev"}
    with pytest.raises(ValueError, match="not \\['colour'\\]"):
        load_template(_write(tmp_path / "bad.yaml", "type: battery\nparams: {}\ncolour: red\n"))
    with pytest.raises(ValueError, match="exactly one device"):
        load_template(_write(tmp_path / "two.yaml", "devices: [{type: solar, name: a}, {type: solar, name: b}]\n"))


def test_a_scenario_device_from_a_template(tmp_path):
    template = _write(tmp_path / "lib" / "managed.yaml", """
type: battery
params: {capacity_kwh: 10, p_max_charge_kw: 5, p_max_discharge_kw: 5, eta_charge: 0.95, degradation_cost_per_kwh: 0.02}
counterfactual: {type: self_consumption}
rules: {dr_event: {action: discharge, to: export}}
""")
    scenario = _write(tmp_path / "home.yaml", """
name: home
time_grid: {resolution_minutes: 60, horizon_steps: 4, start: "2026-07-13T00:00:00"}
devices:
  - {type: battery, name: battery, template: lib/managed.yaml, params: {capacity_kwh: 13.5}}
value_streams:
  - {type: energy_arbitrage, params: {import_price: {type: constant, price: 0.2}, export_price: {type: constant, price: 0.05}}}
external_load: [1, 1, 2, 2]
""")
    assert template.exists()
    raw = ScenarioConfig.model_validate(compose_scenario(scenario).data)
    resolved = resolve_scenario(raw)
    battery = resolved.devices[0]
    assert battery.params["capacity_kwh"] == 13.5 and battery.params["degradation_cost_per_kwh"] == 0.02
    assert battery.sources["capacity_kwh"] == "scenario" and battery.sources["eta_charge"] == "template:managed"
    assert battery.counterfactual == {"type": "self_consumption"} and resolved.has_counterfactual  # R1 from the template
    config = load_scenario_config(scenario)
    assert config.devices[0].template is None and config.counterfactual.policies == {"battery": {"type": "self_consumption"}}
    assert build_premise(raw).devices[0].capacity_kwh == 13.5  # the API resolves too
    rows = {(r["device_id"], r["setting"]): r["source"] for r in resolved.parameter_rows()}
    assert rows[("battery", "eta_charge")] == "template:managed" and rows[("battery", "rules.dr_event")] == "template:managed"


# --- feeders --------------------------------------------------------------------------


def _feeder(houses, defaults=None) -> FeederScenarioConfig:
    return FeederScenarioConfig.model_validate({
        "name": "f", "time_grid": {"resolution_minutes": 60, "horizon_steps": 4, "start": "2026-07-13T00:00:00"},
        "feeder": {"elements": [{"name": "tx", "rating_kw": 50}]},
        "house_defaults": {"value_streams": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "constant", "price": 0.2}, "export_price": {"type": "constant", "price": 0.05}}}],
            **(defaults or {})},
        "houses": houses,
    })


BATTERY = {"type": "battery", "name": "battery", "params": {"capacity_kwh": 10, "p_max_charge_kw": 5, "p_max_discharge_kw": 5}}
SOLAR = {"type": "solar", "name": "solar", "params": {"available_kw": [0, 2, 3, 0]}}


def test_house_defaults_fill_in_per_device_and_setting():
    config = _feeder([{"name": "h1", "element": "tx", "external_load": [1] * 4, "devices": [SOLAR, BATTERY],
                       "poi_limit_kw": 15, "counterfactual": {"policies": {"solar": {"type": "no_flexibility"}}},
                       "rules": {"dr_event": {"battery": {"action": "discharge", "to": "export"}}}}],
                     {"poi_limit_kw": 30, "response": "rules",
                      "counterfactual": {"policies": {"battery": {"type": "self_consumption"}}},
                      "rules": {"dr_event": {"battery": {"action": "discharge", "max_kw": 2}}}})
    (house,), _, _ = resolve_feeder(config)
    assert house.settings["poi_limit_kw"] == 15 and house.sources["poi_limit_kw"] == "houses.h1"
    assert house.settings["response"] == "rules" and house.sources["response"] == "house_defaults"
    assert house.sources["value_streams"] == "house_defaults"
    solar, battery = house.devices
    # the house names only solar's policy; the default fills in the battery's
    assert solar.counterfactual == {"type": "no_flexibility"} and battery.counterfactual == {"type": "self_consumption"}
    assert battery.counterfactual_source == "house_defaults.counterfactual"
    # the same action merges across layers; the house's own layer is the stronger
    assert battery.rules["dr_event"] == {"action": "discharge", "to": "export", "max_kw": 2, "precharge_hours": 0}
    assert battery.rule_sources["dr_event"] == "houses.h1.rules"
    assert house.role().rule_for("dr_event", type("D", (), {"name": "battery"})())["to"] == "export"


def test_a_house_rule_by_type_beats_a_default_by_name():
    config = _feeder([{"name": "h1", "element": "tx", "external_load": [1] * 4, "devices": [BATTERY],
                       "rules": {"dr_event": {"battery": {"action": "none"}}}}],
                     {"rules": {"dr_event": {"battery": {"action": "discharge", "to": "export"}}}})
    (house,), _, _ = resolve_feeder(config)
    assert "dr_event" not in house.devices[0].rules  # `none` from the stronger layer: no rule


def test_premises_file_devices_record_each_layer():
    config = load_feeder_config(REPO / "examples/scenarios/feeder_from_files.yaml")
    resolved, elements, _ = resolve_feeder(config)
    home = next(r for r in resolved if r.premise_id == "home_06")
    battery = next(d for d in home.devices if d.device_id == "battery")
    assert battery.sources["capacity_kwh"] == "premises.csv row 7"
    assert battery.sources["eta_charge"] == "house_defaults.device_params.battery"
    assert battery.sources["soc_init_frac"] == "built-in (v1.1 premises device defaults)"
    assert home.origin == "premises.csv" and elements is not None


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_resolves_and_resolving_again_changes_nothing(path):
    data = compose_scenario(path).data
    if "feeder" in data:
        if "bank" in path.stem:
            pytest.importorskip("xarray")
        built = build_feeder_scenario(load_feeder_config(path))
        assert len(built.resolved) == len(built.premises)
        configs = built.house_configs
    else:
        configs = [load_scenario_config(path)]
    for config in configs:
        again = resolve_scenario(config).to_scenario_config(config.time_grid)
        assert again.model_dump() == config.model_dump()
