"""Studies, phase 3 (docs/POPULATION_MODEL_SPEC.md sections 3-4a): a population with
defaults (where selectors) and overrides, the feeder analysis, and the portfolio
analysis (DR, screening, cost tests per premise)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.population.compile import PopulationError, resolve_study
from der_opt.population.portfolio import portfolio_premises
from der_opt.population.report import population_problems
from der_opt.scenarios import load_scenario_config, run_feeder_study
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.feeder import feeder_from_study
from der_opt.scenarios.schema import StudyConfig
from der_opt.scenarios.validate import validate_scenario

REPO = Path(__file__).parent.parent.parent
DATA = REPO / "examples" / "data"
LIBRARY = REPO / "examples" / "library"
HOME = DATA / "homes" / "annual_home.csv"
runner = CliRunner()


def _two_weeks(folder: Path) -> Path:
    """Two July weeks of the sample home, as two premises with solar and a battery."""
    source = pd.read_csv(HOME)
    stamps = pd.to_datetime(source["timestamp"], utc=True).dt.tz_convert("America/Los_Angeles")
    july = source[(stamps >= "2026-07-01") & (stamps < "2026-07-15")]
    frames = []
    for premise, scale in (("home_a", 1.0), ("home_b", 1.4)):
        frames.append(pd.DataFrame({"timestamp": july["timestamp"], "premise_id": premise, "device_id": "",
                                    "load_kw": (july["load_kw"] * scale).round(4)}))
        frames.append(pd.DataFrame({"timestamp": july["timestamp"], "premise_id": premise, "device_id": "solar",
                                    "pv_kw": july["pv_kw"].round(4)}))
    path = folder / "timeseries.csv"
    pd.concat(frames).sort_values(["premise_id", "device_id", "timestamp"]).to_csv(path, index=False)
    return path


def _study(tmp_path: Path, **changes) -> Path:
    folder = tmp_path / "study"
    folder.mkdir(parents=True, exist_ok=True)
    _two_weeks(folder)
    (folder / "premises.csv").write_text(
        "premise_id,element_id,customer_class,tariff,export_program,segment\n"
        "home_a,tx,residential,pge_e_elec_2026-06,nbt,early\n"
        "home_b,tx,residential,pge_e_elec_2026-06,nbt,late\n", encoding="utf-8")
    (folder / "devices.csv").write_text(
        "premise_id,device_id,type,template,kw,kwh,installed_cost\n"
        "home_a,solar,solar,,5,,\n"
        "home_a,battery,battery,battery_13kwh,5,13.5,9500\n"
        "home_b,solar,solar,,5,,\n"
        "home_b,battery,battery,battery_13kwh,5,13.5,9500\n", encoding="utf-8")
    study = {
        "name": "two_homes",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 336, "start": "2026-07-01T00:00:00"},
        "population": {"premises": "premises.csv", "devices": "devices.csv", "timeseries": ["timeseries.csv"],
                       "templates": [str(LIBRARY / "devices")]},
        "tariffs": {"dir": str(DATA / "tariffs"),
                    "programs": {"nbt": {"export_rates_csv": str(DATA / "export_rates/pge_nbt_eec_2026.csv"), "acc_plus_per_kwh": 0.0088}}},
        "objective": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "retail_tariff", "path": str(DATA / "tariffs" / "pge_e_elec_2026-06.yaml")},
            "export_price": {"type": "constant", "price": 0.05}}}],
        "defaults": [
            {"set": {"premise": {"poi_limit_kw": 20}}},
            {"where": {"type": "battery"}, "set": {"device": {"counterfactual": {"type": "self_consumption"}}}},
            {"where": {"type": "battery", "segment": "late"}, "set": {"device": {"degradation_cost_per_kwh": 0.03}}},
        ],
        "overrides": {"home_a/battery": {"device": {"installed_cost": 9000, "eta_charge": 0.9}},
                      "home_b": {"premise": {"participant_upfront": 250}}},
        "analyses": {
            "feeder": {"elements": [{"name": "tx", "rating_kw": 12, "overload_cost_per_kwh": 1.0}],
                       "coordination": {"envelopes": {}}},
            "portfolio": {
                "measure": {"type": ["battery"]},
                "dr": {"program": {"program_id": "summer", "season": [7], "window": "16:00-21:00", "min_hours": 1,
                                   "max_hours": 3, "response": "incentive", "incentive_per_kwh": 1.0},
                       "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-13", "2026-07-14"]}]}},
                "avoided_costs": {"set": str(DATA / "avoided_costs" / "synthetic_2026")},
            },
        },
    }
    for key, value in changes.items():
        study[key] = value
    path = folder / "study.yaml"
    path.write_text(yaml.safe_dump(study, sort_keys=False), encoding="utf-8")
    return path


def _load(path: Path) -> StudyConfig:
    return StudyConfig.model_validate(compose_scenario(path).data)


def test_defaults_select_and_overrides_win(tmp_path):
    resolved, _, _ = resolve_study(_load(_study(tmp_path)))
    by = {r.premise_id: {d.device_id: d for d in r.devices} for r in resolved}
    a, b = by["home_a"]["battery"], by["home_b"]["battery"]
    assert a.params["eta_charge"] == 0.9 and a.sources["eta_charge"] == "override:home_a/battery"
    assert a.costs == {"installed_cost": 9000} and b.costs == {"installed_cost": 9500}
    assert b.params["degradation_cost_per_kwh"] == 0.03 and b.sources["degradation_cost_per_kwh"] == "defaults[2]"
    assert a.params["degradation_cost_per_kwh"] == 0.015 and a.sources["degradation_cost_per_kwh"] == "template:battery_13kwh"
    assert a.counterfactual == {"type": "self_consumption"} and a.counterfactual_source == "defaults[1]"
    assert by["home_a"]["solar"].counterfactual is None  # the where picks batteries only
    premise = next(r for r in resolved if r.premise_id == "home_b")
    assert premise.settings["poi_limit_kw"] == 20 and premise.sources["poi_limit_kw"] == "defaults[0]"
    assert premise.settings["value_streams"][0]["type"] == "energy_arbitrage" and premise.sources["value_streams"] == "objective"
    assert premise.settings["participant_upfront"] == 250


@pytest.mark.parametrize("change, message", [
    ({"overrides": {"home_c": {"premise": {"poi_limit_kw": 5}}}}, "home_c: overrides a premise that isn't in"),
    ({"overrides": {"home_a/ev": {"device": {"eta_charge": 0.9}}}}, "overrides a device home_a doesn't have"),
    ({"defaults": [{"where": {"type": "battery"}, "set": {"device": {"colour": "red"}}}]}, "has no parameter(s) ['colour']"),
])
def test_study_mistakes_are_population_problems(tmp_path, change, message):
    with pytest.raises(PopulationError) as caught:
        resolve_study(_load(_study(tmp_path, **change)))
    assert any(message in p for p in caught.value.problems), caught.value.problems


def test_a_counterfactual_that_doesnt_fit_its_device_is_caught(tmp_path):
    study = _load(_study(tmp_path, defaults=[{"set": {"device": {"counterfactual": {"type": "self_consumption"}}}}]))
    resolved, _, _ = resolve_study(study)
    problems = population_problems(resolved)
    assert any("home_a/solar: a solar can't take counterfactual 'self_consumption'" in p for p in problems)


def test_the_feeder_analysis_runs_on_the_population(tmp_path):
    built = feeder_from_study(_load(_study(tmp_path)))
    assert {p.name for p in built.premises} == {"home_a", "home_b"} and built.config.coordination is not None
    study = run_feeder_study(built, counterfactual=False)
    assert "envelopes" in study.runs and not study.checks.errors


def test_portfolio_premises_get_their_measure_class_and_costs(tmp_path):
    study = _load(_study(tmp_path))
    analysis = study.analyses.portfolio
    analysis.cost_test = {"start_year": 2027, "measure_life_years": 10, "participant_costs": {"upfront": 100},
                          "avoided_costs": {"set": str(DATA / "avoided_costs" / "synthetic_2026")},
                          "without": {"battery": "removed"}}  # the comparison home has no battery: the measure buys it
    premises, problems = portfolio_premises(study)
    assert not problems
    by = {p.premise_id: p for p in premises}
    assert by["home_a"].measure == ["battery"] and by["home_a"].config.dr.measure_devices == ["battery"]
    assert by["home_a"].config.dr.customer_class == "residential"
    assert by["home_a"].config.cost_test.participant_costs.upfront == 100 + 9000  # base + its battery
    assert by["home_b"].config.cost_test.participant_costs.upfront == 100 + 9500 + 250  # + its own participant_upfront
    analysis.cost_test["without"] = {"battery": "rule_based"}  # compared against the same battery: nothing bought
    by = {p.premise_id: p for p in portfolio_premises(study)[0]}
    assert by["home_a"].config.cost_test.participant_costs.upfront == 100
    analysis.cost_test["without"] = {}
    assert any("needs a mode for battery" in p for p in portfolio_premises(study)[1])
    analysis.cost_test["without"] = {"battery": "removed"}
    analysis.select = {"segment": "late"}
    analysis.measure = {"template": ["battery_13kwh"]}
    premises, _ = portfolio_premises(study)
    assert [p.premise_id for p in premises] == ["home_b"]
    analysis.select, analysis.measure = {}, {"type": ["ev"]}
    premises, _ = portfolio_premises(study)
    assert all(p.config is None and "no devices match" in p.note for p in premises)


def test_dr_needs_each_premises_customer_class(tmp_path):
    path = _study(tmp_path)
    (path.parent / "premises.csv").write_text(
        "premise_id,element_id,tariff,export_program\nhome_a,tx,pge_e_elec_2026-06,nbt\n"
        "home_b,tx,pge_e_elec_2026-06,nbt\n", encoding="utf-8")
    report = validate_scenario(path).report
    assert any("DR needs its customer_class" in f.message and f.premise == "home_a" for f in report.errors)


def test_cost_tests_need_a_whole_year(tmp_path):
    path = _study(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["analyses"]["portfolio"]["cost_test"] = {
        "start_year": 2027, "measure_life_years": 10, "avoided_costs": {"set": str(DATA / "avoided_costs" / "synthetic_2026")},
        "discount_rates": {"utility": 0.075, "participant": 0.1, "societal": 0.03}, "without": {"battery": "rule_based"}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = validate_scenario(path).report
    assert any("cost_test needs a run covering all of 2026" in f.message for f in report.errors)


def test_a_study_reproduces_the_dr_battery_example(tmp_path):
    """The example's home as population tables: the portfolio's scenario for it is the
    example's, setting for setting."""
    folder = tmp_path / "dr"
    folder.mkdir()
    (folder / "premises.csv").write_text("premise_id,customer_class\nhome_a,residential\n", encoding="utf-8")
    (folder / "devices.csv").write_text("premise_id,device_id,type,template,kw,kwh\n"
                                        "home_a,battery,battery,battery_13kwh,5,13.5\nhome_a,solar,solar,,,\n",
                                        encoding="utf-8")
    program = yaml.safe_load((LIBRARY / "programs" / "summer_peak_incentive.yaml").read_text(encoding="utf-8"))["dr"]
    for key in ("customer_class", "measure_devices"):
        program.pop(key)
    cost = compose_scenario(LIBRARY / "cost_tests" / "synthetic_2026.yaml").data["cost_test"]
    study = {
        "name": "dr_battery_study", "weather_case": "synthetic typical year (illustrative)",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 8760, "start": "2026-01-01T00:00:00"},
        "population": {"premises": "premises.csv", "devices": "devices.csv", "timeseries": [str(HOME)],
                       "templates": [str(LIBRARY / "devices")]},
        "objective": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "retail_tariff", "path": str(DATA / "tariffs" / "pge_e_elec_2026-06.yaml")},
            "export_price": {"type": "constant", "price": 0.05}}}],
        "defaults": [{"set": {"premise": {"poi_limit_kw": 20}}},
                     {"where": {"type": "battery"}, "set": {"device": {"counterfactual": {"type": "self_consumption"}}}}],
        "analyses": {"portfolio": {"measure": {"type": ["battery"]}, "dr": program, "cost_test": cost}},
    }
    path = folder / "study.yaml"
    path.write_text(yaml.safe_dump(study), encoding="utf-8")
    (premise,), problems = portfolio_premises(_load(path))
    assert not problems
    mine = premise.config.model_dump(mode="json")
    example = load_scenario_config(REPO / "examples" / "scenarios" / "dr_battery_annual.yaml").model_dump(mode="json")

    def same(data):
        data.pop("name")
        text = json.dumps(data, sort_keys=True).replace(', "tz": "America/Los_Angeles"', "")  # the study names it
        return json.loads(text)

    assert same(mine) == same(example)


def test_cli_runs_both_analyses(tmp_path):
    path = _study(tmp_path)
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "=== Feeder analysis" in result.output and "=== Portfolio analysis" in result.output
    for name in ("premises.csv", "device_parameters.csv", "run_record.json", "feeder/coordination.csv",
                 "portfolio/portfolio.csv", "portfolio/home_a/report.txt", "portfolio/home_b/dr_capacity.csv"):
        assert (out / name).exists(), name
    table = pd.read_csv(out / "portfolio" / "portfolio.csv")
    assert list(table["premise_id"]) == ["home_a", "home_b", "total"]
    assert table.iloc[-1]["dr_incentive_earned"] == pytest.approx(table.iloc[:2]["dr_incentive_earned"].sum(), abs=0.01)
    # each comparison case's bill, and a study-wide pair selector (cases spec section 6)
    assert {"case_bill_no_devices", "case_bill_without", "case_bill_optimized", "case_bill_responding"} <= set(table)
    assert table.loc[0, "case_bill_optimized"] == pytest.approx(table.loc[0, "bill_optimal"], abs=0.01)
    assert '"study_cases"' in (out / "report.html").read_text(encoding="utf-8")
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert record["results"]["analyses"] == ["feeder", "portfolio"]
    assert any(i["path"].endswith("devices.csv") for i in record["inputs"])


def test_a_study_names_unmatched_without_entries_once_not_per_home(tmp_path):
    """A study-wide `without` may name a type only some homes have; it's flagged only
    when no home has it, once for the study."""
    path = _study(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["analyses"]["portfolio"]["cost_test"] = {
        "start_year": 2027, "measure_life_years": 10, "avoided_costs": {"set": str(DATA / "avoided_costs" / "synthetic_2026")},
        "discount_rates": {"utility": 0.075, "participant": 0.1, "societal": 0.03},
        "without": {"battery": "rule_based", "ev": "tou_timer"}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    found = [f for f in validate_scenario(path).report.findings if "match no device" in f.message]
    assert len(found) == 1 and found[0].premise is None and "['ev']" in found[0].message
