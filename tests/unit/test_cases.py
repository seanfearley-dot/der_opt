"""Comparison cases (docs/COMPARISON_CASES_SPEC.md v1.0): which a run makes, their
names, pricing, what's written, the report's selector, and the study's columns."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cases import (
    CasesError,
    buttons,
    labels,
    opening_pair,
    planned,
    unavailable,
)
from der_opt.cli.main import app
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.schema import ScenarioConfig
from der_opt.scenarios.validate import validate_scenario

REPO = Path(__file__).parent.parent.parent
LIBRARY = REPO / "examples" / "library"
DATA = REPO / "examples" / "data"
HOME = DATA / "spec_sample" / "annual_home.csv"
runner = CliRunner()


def _scenario(tmp_path: Path, **changes) -> Path:
    """A July week of the sample home: solar and a battery, billed on E-ELEC with NBT,
    a self-consuming counterfactual, and a DR program whose measure is the battery."""
    scenario = {
        "extends": [str(LIBRARY / name) for name in (
            "devices/battery_13kwh.yaml", "value_streams/bill.yaml", "meter/panel_20kw.yaml",
            "counterfactual/battery_self_consumption.yaml", "billing/pge_e_elec_2026-06_nbt.yaml",
            "programs/summer_peak_incentive.yaml")],
        "name": "week",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 168, "start": "2026-07-13T00:00:00"},
        "devices": [{"type": "solar", "name": "solar", "params": {"available_kw": {
            "source": "timeseries", "path": str(HOME), "premise_id": "home_a", "column": "pv_kw"}}}],
        "external_load": {"source": "timeseries", "path": str(HOME), "premise_id": "home_a", "column": "load_kw"},
        "dr": {"shadow_events": False, "events": {"random": None, "schedule": [
            {"start": "17:00", "hours": 2, "dates": ["2026-07-16"]}]}},
    }
    scenario.update(changes)
    path = tmp_path / "week.yaml"
    path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return path


def _config(path: Path) -> ScenarioConfig:
    return ScenarioConfig.model_validate(compose_scenario(path).data)


def test_the_cases_a_run_can_make_and_their_names(tmp_path):
    config = _config(_scenario(tmp_path))
    assert planned(config) == ["no_devices", "no_measure", "rule_based", "optimized", "responding"]
    named = labels(config)
    assert named["no_measure"][0] == "Solar only" and named["rule_based"][0] == "Battery, rule-based"
    assert named["responding"][0] == "Optimized + DR"
    assert [b["label"] for b in buttons(config, planned(config))] == [
        "Solar", "The battery, rule-based", "Optimization", "DR participation", "Everything"]
    assert opening_pair(config, planned(config)) == ("rule_based", "optimized", "the value of optimization")

    bare = config.model_copy(update={"dr": None, "counterfactual": None})
    assert planned(bare) == ["no_devices", "optimized"]  # the measure is every device: no no_measure
    assert "no counterfactual" in unavailable(bare)["rule_based"]
    assert planned(config, counterfactual=False) == ["no_devices", "no_measure", "optimized", "responding"]


def test_cases_settings_are_checked(tmp_path):
    wrong = _config(_scenario(tmp_path, cases={"include": ["optimized", "responding"]}, dr=None))
    with pytest.raises(CasesError, match="cases.include names responding"):
        planned(wrong.model_copy(update={"dr": None}))
    path = _scenario(tmp_path, cases={"include": ["no_devices", "optimized"],
                                      "compare": {"without": "rule_based", "with": "optimized"}})
    found = [f for f in validate_scenario(path).report.findings if f.check == "cases"]
    assert found and "cases.compare names rule_based" in found[0].message


def test_a_run_makes_prices_and_writes_every_case(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(_scenario(tmp_path)), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "Cases (each priced the same way" in result.output and "Solar only" in result.output
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["summary_version"] == "1.1"
    cases = summary["cases"]
    assert list(cases) == ["no_devices", "no_measure", "rule_based", "optimized", "responding"]
    assert cases["optimized"]["costs"] == pytest.approx(summary["costs"]["optimal"])
    assert cases["rule_based"]["costs"] == pytest.approx(summary["costs"]["counterfactual"])
    assert cases["optimized"]["bill"]["net_cost"] == pytest.approx(summary["bills"]["optimal"]["net_cost"])
    assert cases["responding"]["dr_incentive"] is not None and cases["optimized"]["dr_incentive"] is None
    assert "DREventResponse" not in cases["responding"]["costs"]  # the incentive is its own line
    assert cases["no_devices"]["devices"] == [] and cases["no_measure"]["devices"] == ["solar"]
    assert "battery degradation" not in cases["no_measure"]["costs"]
    assert cases["no_measure"]["energy"]["self_consumed_solar_kwh"] <= cases["no_measure"]["energy"]["solar_kwh"]
    assert summary["case_compare"]["without"] == "rule_based" and summary["case_compare"]["with"] == "optimized"

    net = pd.read_csv(out / "cases_net_load_kw.csv", index_col=0, parse_dates=True)
    load = pd.read_csv(HOME, parse_dates=["timestamp"])
    assert list(net.columns) == list(cases) and len(net) == 168
    assert (net["no_devices"] > 0).all()  # the load alone: no export
    imported = cases["no_devices"]["energy"]["import_kwh"]
    assert imported == pytest.approx(net["no_devices"].sum(), rel=1e-3)
    assert load["load_kw"].max() > 0
    dr = pd.read_csv(out / "dr_net_load_kw.csv", index_col=0)  # DR's references: the same runs
    assert (dr["R3"].to_numpy() - net["no_measure"].to_numpy()).max() == pytest.approx(0, abs=1e-3)
    assert (dr["R1"].to_numpy() - net["rule_based"].to_numpy()).max() == pytest.approx(0, abs=1e-3)
    assert (dr["replicate_0"].to_numpy() - net["responding"].to_numpy()).max() == pytest.approx(0, abs=1e-3)

    table = pd.read_csv(out / "cases.csv")
    whole = table[table["period"] == "run"].set_index("case")
    assert whole.loc["optimized", "bill"] == pytest.approx(cases["optimized"]["bill"]["net_cost"])
    assert "cost: NBTBill" in table and set(table["case"]) == set(cases)

    devices = pd.read_csv(out / "cases_device_dispatch.csv", index_col=0)
    assert {"optimized.battery.soc", "responding.battery.net_power", "no_measure.solar.net_power"} <= set(devices)
    assert not any(c.startswith(("no_devices.", "no_measure.battery")) for c in devices)

    page = (out / "report.html").read_text(encoding="utf-8")
    assert page.count('class="case-pick"') == 2  # one A/B choice for the whole page, pinned at the top
    assert 'class="pairbar"' in page and "Differences are A − B" in page and "DR participation" in page
    assert "Not set by A and B" in page  # DR doesn't follow the choice, and says so
    order = [page.index(f'<section id="{name}">') for name in ("summary", "dispatch", "energy", "bills", "dr", "value",
                                                                 "checks", "settings")]
    assert order == sorted(order)  # the page's order
    assert '"dr_events":1' in page and "During DR events" in page  # the summary card over the event hours
    # the DR section: an event's day with the settlement baseline, and capacity against two references
    assert dr["S_replicate_0"].notna().sum() == 2 and dr["S_replicate_0"].loc["2026-07-16 17:00":"2026-07-16 18:00"].notna().all()
    assert "An event&#x27;s day" in page and "Thu Jul 16, 17:00" in page
    assert "vs settlement baseline, accounted" in page and "vs Solar only" not in page  # R3 is left out


def test_a_case_can_be_billed_on_its_own_tariff(tmp_path):
    block = tmp_path / "tou_c.yaml"
    block.write_text(yaml.safe_dump({"billing": {"tariff": str(DATA / "tariffs" / "pge_e_tou_c_2026-06.yaml"),
                                                 "baseline_territory": "X"}}), encoding="utf-8")
    path = _scenario(tmp_path, dr=None, cases={"no_devices": {"billing": "tou_c.yaml"}})
    assert not [f for f in validate_scenario(path).report.findings if f.check == "cases"]
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    cases = json.loads((out / "summary.json").read_text(encoding="utf-8"))["cases"]
    assert "E-TOU-C" in cases["no_devices"]["label"] and cases["no_devices"]["tariff"] != cases["optimized"]["tariff"]
    assert cases["no_devices"]["program"] is None and cases["optimized"]["program"] == "nbt"
