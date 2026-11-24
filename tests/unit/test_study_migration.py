"""Population model phase 4 (docs/POPULATION_MODEL_SPEC.md sections 3a and 6):
add_devices, premises written out in a study, the old names read with warnings, and
the examples migrated to studies, each resolving to the premises its old form did."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.population.compile import PopulationError, resolve_study
from der_opt.scenarios import (
    build_feeder_scenario,
    build_premise,
    feeder_from_study,
    load_feeder_config,
    load_study_config,
)
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.library import read_library
from der_opt.scenarios.validate import validate_scenario

REPO = Path(__file__).parent.parent.parent
DATA = REPO / "examples" / "data"
SAMPLE = DATA / "spec_sample"
OLD = REPO / "tests" / "fixtures" / "old_forms"
runner = CliRunner()


def _study(tmp_path: Path, **changes) -> Path:
    """The spec sample as a study: home_05 is metered, home_06 has the only battery,
    home_04 and home_06 have no EV."""
    study = {
        "name": "sample", "extends": [str(REPO / "examples/library/time/2026-07-13T12_72x60min.yaml")],
        "population": {"premises": str(SAMPLE / "premises.csv"), "devices": str(SAMPLE / "devices.csv"),
                       "timeseries": [str(SAMPLE / "timeseries.csv")], "network": str(SAMPLE / "network.csv")},
        "tariffs": {"dir": str(DATA / "tariffs"),
                    "programs": {"nbt": {"export_rates_csv": str(DATA / "export_rates/pge_nbt_eec_2026.csv"),
                                         "acc_plus_per_kwh": 0.0088}, "nem2": {"nsc_rate_per_kwh": 0.02781}}},
        "objective": "bill",
        "analyses": {"feeder": {}},
    }
    study.update(changes)
    path = tmp_path / "study.yaml"
    path.write_text(yaml.safe_dump(study, sort_keys=False), encoding="utf-8")
    return path


BATTERY = {"where": {"kind": "premise"}, "share": 0.5, "seed": 1,
           "device": {"device_id": "battery", "type": "battery", "template": "battery_13kwh", "kw": 5, "kwh": 13.5,
                      "installed_cost": 9000}}


def _devices(resolved) -> dict[str, set[str]]:
    return {r.premise_id: {d.device_id for d in r.devices} for r in resolved}


# --- add_devices ---------------------------------------------------------------------


def test_add_devices_picks_an_exact_share_of_the_eligible(tmp_path):
    details: dict = {}
    resolved, _, _ = resolve_study(load_study_config(_study(tmp_path, add_devices=[BATTERY])), details)
    (entry,) = details["add_devices"]
    # home_06 already has one, home_05 is metered: four eligible, half of them chosen
    assert entry["already_have"] == ["home_06"] and entry["metered"] == ["home_05"] and entry["eligible"] == 4
    assert len(entry["chosen"]) == 2
    devices = _devices(resolved)
    assert {p for p in entry["chosen"]} == {p for p in ("home_01", "home_02", "home_03", "home_04")
                                            if "battery" in devices[p]}
    home = next(r for r in resolved if r.premise_id == entry["chosen"][0])
    battery = next(d for d in home.devices if d.device_id == "battery")
    assert battery.sources["capacity_kwh"] == "add_devices[0]" and battery.template == "battery_13kwh"
    assert battery.costs == {"installed_cost": 9000.0}
    assert battery.sources["degradation_cost_per_kwh"] == "template:battery_13kwh"


def test_raising_a_share_keeps_the_premises_chosen_and_entries_are_independent(tmp_path):
    def chosen(*entries):
        details: dict = {}
        resolve_study(load_study_config(_study(tmp_path, add_devices=list(entries))), details)
        return {e["device_id"]: e["chosen"] for e in details["add_devices"]}

    half = chosen(BATTERY)["battery"]
    everyone = chosen({**BATTERY, "share": 1.0})["battery"]
    assert set(half) < set(everyone) and len(everyone) == 4
    ev = {"share": 1.0, "seed": 7, "device": {"device_id": "ev", "type": "ev", "template": "ev_commuter_v2x",
                                              "kw": 7.2, "kwh": 75, "v2x": 1}}
    both = chosen(ev, BATTERY)
    assert both["battery"] == half  # another entry doesn't change which premises this one picks
    assert both["ev"] == ["home_04", "home_06"]  # the premises without an EV, metered home_05 aside


def test_added_devices_take_defaults_and_overrides_and_their_series(tmp_path):
    shape = {"source": "timeseries", "path": str(SAMPLE / "timeseries.csv"), "premise_id": "home_01",
             "column": "pv_kw", "per_kw": True}
    solar = {"where": {"premise_id": "home_04"}, "share": 1.0,
             "device": {"device_id": "solar", "type": "solar", "kw": 2.0}}
    ev = {"where": {"premise_id": "home_04"}, "share": 1.0,
          "device": {"device_id": "ev", "type": "ev", "template": "ev_commuter_v2x", "kw": 7.2, "kwh": 75, "v2x": 1}}
    path = _study(tmp_path, add_devices=[solar, ev],
                  defaults=[{"where": {"type": "solar"}, "set": {"device": {"available_kw": shape}}}],
                  overrides={"home_04/ev": {"device": {"soc_init_frac": 0.5}}})
    study = load_study_config(path)
    resolved, _, _ = resolve_study(study)
    home = next(r for r in resolved if r.premise_id == "home_04")
    devices = {d.device_id: d for d in home.devices}
    assert devices["solar"].params["available_kw"]["multiply"] == 2.0  # the shared shape, sized by kw
    assert devices["ev"].params["pattern"]["weekday"]["miles"] == 38  # its template's pattern: no series needed
    assert devices["ev"].params["soc_init_frac"] == 0.5 and devices["ev"].sources["soc_init_frac"] == "override:home_04/ev"
    other = next(r for r in resolved if r.premise_id == "home_01")
    assert "multiply" not in str(next(d for d in other.devices if d.device_id == "solar").params["available_kw"])
    built = build_premise(home.to_scenario_config(study.time_grid))
    reference = build_premise(other.to_scenario_config(study.time_grid))
    pv = {p.name: p for p in built.devices}["solar"].available_kw
    assert np.allclose(pv, 2.0 * np.asarray({p.name: p for p in reference.devices}["solar"].available_kw))


def test_add_devices_mistakes_are_population_problems(tmp_path):
    solar = {"share": 1.0, "device": {"device_id": "solar2", "type": "solar", "kw": 3}}  # no series for it
    with pytest.raises(PopulationError) as caught:
        resolve_study(load_study_config(_study(tmp_path, add_devices=[solar])))
    assert any("home_02/solar2: a solar needs a pv_kw series" in p for p in caught.value.problems)
    data = yaml.safe_load(_study(tmp_path).read_text(encoding="utf-8"))
    data["population"].pop("devices")
    path = tmp_path / "v11.yaml"
    data["population"]["premises"] = str(REPO / "tests/fixtures/v11/spec_sample/premises.csv")
    path.write_text(yaml.safe_dump({**data, "add_devices": [BATTERY]}), encoding="utf-8")
    with pytest.raises(PopulationError, match="add_devices: adds rows to the population's devices file"):
        resolve_study(load_study_config(path))


def test_run_reports_what_add_devices_chose(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(_study(tmp_path, add_devices=[BATTERY])), "-o", str(out),
                                 "--no-counterfactual"])
    assert result.exit_code == 0, result.output
    assert "add_devices[0]: battery added at 2 of 4 eligible premises (6 selected, 1 already had one, 1 metered)" \
        in result.output
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert len(record["results"]["add_devices"][0]["chosen"]) == 2
    assert "add_devices[0]" in (out / "device_parameters.csv").read_text(encoding="utf-8")


# --- premises written out in a study -------------------------------------------------


def test_written_out_premises_take_defaults_tags_tariffs_and_overrides(tmp_path):
    load = {"source": "timeseries", "path": str(SAMPLE / "timeseries.csv"), "premise_id": "home_04", "column": "load_kw"}
    premises = [
        {"name": "shop", "element": "tx_2", "tariff": "pge_e_elec_2026-06", "export_program": "nbt",
         "customer_class": "commercial", "tags": {"segment": "retail"}, "external_load": load,
         "devices": [{"type": "battery", "name": "battery", "template": "battery_13kwh"}]},
        {"name": "garage", "element": "tx_2", "kind": "background", "external_load": [1.0] * 72},
    ]
    path = _study(tmp_path, premises=premises,
                  defaults=[{"where": {"segment": "retail"}, "set": {"premise": {"poi_limit_kw": 50},
                                                                     "device": {"soc_init_frac": 0.2}}}],
                  overrides={"shop/battery": {"device": {"installed_cost": 7000}}})
    resolved, _, background = resolve_study(load_study_config(path))
    shop = next(r for r in resolved if r.premise_id == "shop")
    assert shop.origin == "written out" and shop.settings["poi_limit_kw"] == 50
    assert shop.settings["billing"]["tariff"] == str(DATA / "tariffs" / "pge_e_elec_2026-06.yaml")
    assert shop.settings["billing"]["program"]["type"] == "nbt" and shop.settings["customer_class"] == "commercial"
    battery = shop.devices[0]
    assert battery.params["soc_init_frac"] == 0.2 and battery.sources["soc_init_frac"] == "defaults[0]"
    assert battery.costs == {"installed_cost": 7000}
    assert background["garage"][0] == "tx_2" and "garage" not in {r.premise_id for r in resolved}
    built = feeder_from_study(load_study_config(path))
    assert "garage" in built.background and len(built.premises) == 7


def test_a_written_out_premise_can_repeat_no_premises_file_id(tmp_path):
    premises = [{"name": "home_01", "element": "tx_1", "external_load": [1.0] * 72}]
    with pytest.raises(PopulationError, match="written out in the study and in the population's files"):
        resolve_study(load_study_config(_study(tmp_path, premises=premises)))


def test_a_study_needs_a_population_or_premises(tmp_path):
    data = yaml.safe_load(_study(tmp_path).read_text(encoding="utf-8"))
    data.pop("population")
    path = tmp_path / "empty.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = validate_scenario(path).report
    assert any("needs a population (its files) or premises written out" in f.message for f in report.errors)


# --- migration ------------------------------------------------------------------------


@pytest.mark.parametrize("example", ["feeder_from_files", "feeder_ev_heat_wave", "feeder_coordination",
                                     "feeder_bank_planning_trace"])
def test_each_example_resolves_as_its_old_form_did(example):
    if "bank" in example:
        pytest.importorskip("xarray")
    old = build_feeder_scenario(load_feeder_config(OLD / f"{example}.yaml"))
    new = feeder_from_study(load_study_config(REPO / "examples" / "scenarios" / f"{example}.yaml"))

    def configs(built):
        out = [c.model_dump(mode="json") for c in built.house_configs]
        for config in out:
            for device in config["devices"]:
                device.pop("template", None)
        return out

    assert configs(old) == configs(new)
    assert {k: v.__dict__ for k, v in old.roles.items()} == {k: v.__dict__ for k, v in new.roles.items()}
    assert {k: (e, list(np.round(kw, 9))) for k, (e, kw) in old.background.items()} == \
        {k: (e, list(np.round(kw, 9))) for k, (e, kw) in new.background.items()}
    assert old.explicit_rules == new.explicit_rules


def test_old_names_warn_and_the_examples_use_none(tmp_path):
    report = validate_scenario(OLD / "feeder_coordination.yaml").report
    warned = {f.message.split(" ")[0] for f in report.warnings if f.check == "old names"}
    assert warned == {"houses", "house_defaults", "feeder.background_load"}
    assert not report.errors
    for path in sorted((REPO / "examples" / "scenarios").glob("*.yaml")):
        data = compose_scenario(path).data
        assert not {"houses", "house_defaults", "premise_data"} & set(data), path.name


def test_study_defaults_add_up_across_extends(tmp_path):
    data = compose_scenario(REPO / "examples" / "scenarios" / "feeder_from_files.yaml").data
    wheres = [d.get("where") for d in data["defaults"]]
    # homes_bill's two, device_params's two, then the study's own two (EVs on plug-in, and every premise)
    assert wheres == [{"type": "ev"}, {"type": "battery"}, {"type": "battery"}, {"type": "ev"}, {"type": "ev"}, None]
    kinds = {b.name: b.kind for b in read_library(REPO / "examples" / "library")}
    assert kinds["study/homes_bill"] == kinds["study/device_params"] == "study"
