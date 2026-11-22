"""Feeder coordination (docs/COORDINATION_SPEC.md v1.0): participation and background
load, envelopes, the price signal, rule-based responses, feeder DR events, twins, and
the reported results."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.coordination import (
    Context,
    Inputs,
    RuleSimulator,
    compute_envelopes,
    house_role,
    water_fill,
)
from der_opt.coordination.mechanisms import (
    SignalAdder,
    element_prices,
    participant_adder,
)
from der_opt.coordination.study import check_coordination
from der_opt.devices import (
    BatteryStorage,
    CurtailableLoad,
    SmartThermostat,
    SolarPV,
    V2XVehicle,
)
from der_opt.dr.calendar import loading_triggered
from der_opt.network.topology import Feeder, NetworkElement
from der_opt.premise import Premise
from der_opt.scenarios.feeder import build_feeder_scenario, run_feeder_study
from der_opt.scenarios.schema import (
    DRProgramConfig,
    EnvelopeConfig,
    FeederScenarioConfig,
    HouseDefaultsConfig,
    PriceSignalConfig,
)
from der_opt.simulation import RuleBasedEVChargingPolicy, SetpointThermostatPolicy
from der_opt.time_grid import TimeGrid

DAY = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2026, 7, 13))  # a Monday
INDEX = pd.DatetimeIndex(DAY.timestamps())
runner = CliRunner()


# --- envelopes ------------------------------------------------------------------------


def _two_level(names_a=3, names_b=3):
    attachments = {f"a{i}": "A" for i in range(names_a)} | {f"b{i}": "B" for i in range(names_b)}
    return Feeder("f", [NetworkElement("lateral", 60), NetworkElement("A", 15, parent="lateral"),
                        NetworkElement("B", 60, parent="lateral")], attachments=attachments)


def _inputs(feeder, needs=None, service=None, weights=None, steps=1):
    names = list(feeder.attachments)
    needs = needs or {p: 1.0 for p in names}
    return Inputs(names,
                  {"import_kw": {p: np.full(steps, needs[p]) for p in names},
                   "export_kw": {p: np.full(steps, needs[p]) for p in names}},
                  {"import_kw": service or dict.fromkeys(names), "export_kw": service or dict.fromkeys(names)},
                  weights or dict.fromkeys(names))


def _envelopes(config: dict, feeder, inputs, fixed=None, steps=1) -> dict[str, np.ndarray]:
    frame = compute_envelopes(EnvelopeConfig.model_validate({"min_kw": 0} | config), feeder, inputs, fixed or {},
                              pd.date_range("2026-07-13", periods=steps, freq="h"), 1.0)
    imports = frame[frame["quantity"] == "import_kw"]
    return {p: rows["kw"].to_numpy() for p, rows in imports.groupby("premise_id")}


def test_water_fill_gives_what_a_capped_share_cant_take_to_the_rest():
    assert water_fill(12, np.ones(3), np.array([2, np.inf, np.inf])).tolist() == [2, 5, 5]
    assert water_fill(12, np.array([1, 1, 2]), np.full(3, np.inf)).tolist() == [3, 3, 6]
    assert water_fill(3, np.ones(2), np.array([1, 1])).tolist() == [1, 1]  # headroom left over


def test_nesting_reproduces_the_specs_example():
    feeder = _two_level()
    flat = _envelopes({"import": {"method": "equal"}}, feeder, _inputs(feeder))
    assert {p: v[0] for p, v in flat.items()} == {"a0": 5, "a1": 5, "a2": 5, "b0": 10, "b1": 10, "b2": 10}
    top = _envelopes({"import": {"method": "equal"}, "nesting": "top_down"}, feeder, _inputs(feeder))
    assert {p: v[0] for p, v in top.items()} == {"a0": 5, "a1": 5, "a2": 5, "b0": 15, "b1": 15, "b2": 15}


def test_allocation_methods():
    feeder = Feeder("f", [NetworkElement("tx", 12)], attachments={"x": "tx", "y": "tx", "z": "tx"})
    needs = {"x": 1.0, "y": 2.0, "z": 9.0}
    first = lambda cfg, **kw: {p: v[0] for p, v in _envelopes(cfg, feeder, _inputs(feeder, needs, **kw)).items()}
    assert first({}) == {"x": 1, "y": 2, "z": 9}  # pro_rata_need, the default: 12 x need / 12
    assert first({"import": {"method": "equal"}}) == {"x": 4, "y": 4, "z": 4}
    assert first({"import": {"method": "max_min"}}) == {"x": 1, "y": 2, "z": 9}  # needs fit: capped at need
    assert first({"import": {"method": "service_size"}}, service={"x": 10, "y": 10, "z": 20}) == {"x": 3, "y": 3, "z": 6}
    assert first({"import": {"method": "weights"}}, weights={"x": 1, "y": 1, "z": 4}) == {"x": 2, "y": 2, "z": 8}
    assert first({"import": {"method": "fixed", "kw": 5}}) == {"x": 5, "y": 5, "z": 5}
    tight = Feeder("f", [NetworkElement("tx", 6)], attachments={"x": "tx", "y": "tx", "z": "tx"})
    got = {p: v[0] for p, v in _envelopes({"import": {"method": "max_min"}}, tight, _inputs(tight, needs)).items()}
    assert got == {"x": 1, "y": 2, "z": 3}  # x and y need less than 2 each; z gets what's left


def test_headroom_floor_service_cap_and_granularity():
    feeder = Feeder("f", [NetworkElement("tx", 10)], attachments={"x": "tx", "y": "tx"})
    fixed = {"shop": ("tx", np.array([2.0, 6.0, 9.0]))}  # background load through the transformer
    inputs = _inputs(feeder, steps=3)
    step = _envelopes({"import": {"method": "equal"}, "headroom": {"margin_pct": 10}}, feeder, inputs, fixed, steps=3)
    assert step["x"].tolist() == pytest.approx([3.5, 1.5, 0.0])  # (9 - fixed) / 2
    floored = _envelopes({"import": {"method": "equal"}, "min_kw": 1.0}, feeder, inputs, fixed, steps=3)
    assert floored["x"].tolist() == pytest.approx([4.0, 2.0, 1.0])
    static = _envelopes({"import": {"method": "equal"}, "granularity": "static"}, feeder, inputs, fixed, steps=3)
    assert static["x"].tolist() == pytest.approx([0.5, 0.5, 0.5])
    block = _envelopes({"import": {"method": "equal"}, "granularity": {"block_hours": 2}}, feeder, inputs, fixed, steps=3)
    assert block["x"].tolist() == pytest.approx([2.0, 2.0, 0.5])
    capped = _envelopes({"import": {"method": "equal"}}, feeder, _inputs(feeder, service={"x": 1.0, "y": 1.0}, steps=3),
                        fixed, steps=3)
    assert capped["x"].max() == 1.0


def test_envelopes_from_a_file(tmp_path):
    feeder = Feeder("f", [NetworkElement("tx", 10)], attachments={"x": "tx"})
    path = tmp_path / "env.csv"
    path.write_text("timestamp,premise_id,import_kw,export_kw\n2026-07-13 00:00,x,3.5,1\n", encoding="utf-8")
    got = _envelopes({"import": {"method": "file", "path": str(path)}}, feeder, _inputs(feeder))
    assert got["x"].tolist() == [3.5]
    with pytest.raises(ValueError, match="doesn't cover the run"):
        _envelopes({"import": {"method": "file", "path": str(path)}}, feeder, _inputs(feeder, steps=2), steps=2)


# --- price signal ---------------------------------------------------------------------


def test_schedule_signal_sums_along_the_path_and_is_charged_on_net_import():
    feeder = _two_level(1, 1)
    config = PriceSignalConfig.model_validate({"source": "schedule", "schedule": [
        {"element": "A", "per_kwh": 0.5, "hours": "17:00-20:00"},
        {"element": "lateral", "per_kwh": 0.1, "hours": "18:00-19:00", "days": "weekdays"}]})
    prices = element_prices(config, INDEX, feeder, None)
    a, b = participant_adder(prices, feeder, "a0"), participant_adder(prices, feeder, "b0")
    assert a[17] == 0.5 and a[18] == pytest.approx(0.6) and a[16] == 0 and b[18] == pytest.approx(0.1) and b[17] == 0
    adder = SignalAdder(a, DAY)
    imp, exp = np.zeros(24), np.zeros(24)
    imp[17], exp[18] = 2.0, 1.0  # pay at 17:00, earn exporting at 18:00
    assert adder.evaluate_numeric(imp, exp, DAY) == pytest.approx(2 * 0.5 - 1 * 0.6)


# --- rule-based responses ---------------------------------------------------------------


def _role(rules=None, **kw):
    return house_role(HouseDefaultsConfig(), type("H", (), {"rules": rules or {}, **kw})())


def _event(hours):
    mask = np.zeros(24, dtype=bool)
    mask[list(hours)] = True
    return mask


def _context(event=(), **kw):
    from der_opt.coordination.rules import to_event_steps

    mask = _event(event)
    return Context(mask, to_event_steps(mask), **kw)


def _ev(v2x=False, soc_init=0.3):
    return V2XVehicle("ev", DAY, capacity_kwh=60, p_max_charge_kw=7.2, p_max_discharge_kw=7.2 if v2x else 0.0,
                      connected=np.ones(24), soc_init_frac=soc_init, eta_charge=1.0, eta_discharge=1.0)


def _simulate(devices, policies, rules=None, ctx=None, load=1.0):
    """The rule simulator's result, indexed by step number for these checks."""
    from types import SimpleNamespace

    premise = Premise("h", DAY, devices, [], external_load=np.full(24, load))
    result, over = RuleSimulator(premise, policies, _role(rules), ctx or _context()).run()
    by_step = SimpleNamespace(device_dispatch=result.device_dispatch.reset_index(drop=True),
                              grid_import=result.grid_import.reset_index(drop=True),
                              grid_export=result.grid_export.reset_index(drop=True))
    return by_step, over


def test_ev_pauses_for_events_and_catches_up_unless_told_not_to():
    ev = _ev()
    policy = {"ev": RuleBasedEVChargingPolicy(7.2, ev.soc_max)}
    result, _ = _simulate([ev], policy, ctx=_context(range(3)))
    p_ch = result.device_dispatch["ev.p_ch"]
    assert p_ch[:3].sum() == 0 and p_ch[3] == 7.2
    ev2 = _ev()
    result, _ = _simulate([ev2], {"ev": RuleBasedEVChargingPolicy(7.2, ev2.soc_max)},
                          {"dr_event": {"ev": {"action": "pause", "catch_up": False}}}, _context(range(3)))
    assert result.device_dispatch["ev.p_ch"].sum() == 0  # still plugged in, so it stays off


def test_v2x_and_battery_discharge_to_the_home_or_export_with_a_floor():
    ev = _ev(v2x=True, soc_init=0.6)
    rules = {"dr_event": {"ev": {"action": "discharge", "to": "export", "keep_soc_frac": 0.5}}}
    result, _ = _simulate([ev], {}, rules, _context([18, 19]), load=1.0)
    dis = result.device_dispatch["ev.p_dis"]
    assert dis[18] == pytest.approx(6.0) and dis[19] == 0  # 36 kWh down to the 30 kWh floor
    assert result.grid_export[18] == pytest.approx(5.0)
    battery = BatteryStorage("battery", DAY, capacity_kwh=13.5, p_max_charge_kw=5, p_max_discharge_kw=5,
                             eta_charge=1.0, eta_discharge=1.0, soc_init_frac=1.0)
    result, _ = _simulate([battery], {}, None, _context([18]), load=2.5)  # default: discharge to cover the home
    assert result.device_dispatch["battery.p_dis"][18] == pytest.approx(2.5) and result.grid_export[18] == 0
    result, _ = _simulate([battery], {}, {"dr_event": {"battery": {"action": "discharge", "to": "export"}}},
                          _context([18]), load=2.5)
    assert result.grid_export[18] == pytest.approx(2.5)


def test_battery_precharges_before_an_event():
    battery = BatteryStorage("battery", DAY, capacity_kwh=10, p_max_charge_kw=5, p_max_discharge_kw=5,
                             eta_charge=1.0, eta_discharge=1.0, soc_init_frac=0.0)
    rules = {"dr_event": {"battery": {"precharge_hours": 2}}}
    result, _ = _simulate([battery], {}, rules, _context([18, 19]), load=1.0)
    assert result.device_dispatch["battery.p_ch"][16:18].tolist() == [5.0, 5.0]
    assert result.device_dispatch["battery.p_dis"][18] == pytest.approx(1.0)


def test_thermostat_setback_and_flexible_load_shed():
    th = SmartThermostat("thermostat", DAY, thermal_resistance_r=2.5, thermal_capacitance_c=2.0, ambient_temp_c=35,
                         comfort_min_c=20, comfort_max_c=25, cop=3.2, p_max_kw=4, initial_temp_c=25)
    flex = CurtailableLoad("pump", DAY, baseline_load_kw=1.5, block_size_kw=0.75, max_blocks=2)
    result, _ = _simulate([th, flex], {"thermostat": SetpointThermostatPolicy(th)}, None, _context([17, 18]))
    temp = result.device_dispatch["thermostat.temp"]
    assert temp[16] == pytest.approx(25) and temp[19] == pytest.approx(27)  # +2 °C setback during the event
    assert result.device_dispatch["pump.n_blocks"][17] == 2 and result.device_dispatch["pump.n_blocks"][16] == 0


def test_envelope_rules_cut_charging_and_curtail_solar():
    ev = _ev()
    caps = np.full(24, 3.0)
    result, over = _simulate([ev], {"ev": RuleBasedEVChargingPolicy(7.2, ev.soc_max)}, None,
                             _context(import_caps=caps), load=1.0)
    assert result.grid_import.max() == pytest.approx(3.0) and over["import_kwh"] == 0
    _, over = _simulate([], {}, None, _context(import_caps=np.full(24, 0.5)), load=1.0)
    assert over["import_kwh"] == pytest.approx(12.0)  # nothing to cut: reported, not hidden
    solar = SolarPV("solar", DAY, available_kw=np.full(24, 6.0))
    result, _ = _simulate([solar], {}, None, _context(export_caps=np.full(24, 2.0)), load=1.0)
    assert result.grid_export.max() == pytest.approx(2.0)


def test_signal_rules_and_the_less_draw_wins():
    battery = BatteryStorage("battery", DAY, capacity_kwh=13.5, p_max_charge_kw=5, p_max_discharge_kw=5,
                             eta_charge=1.0, eta_discharge=1.0, soc_init_frac=1.0)
    adder = np.zeros(24)
    adder[18] = 0.5
    rules = {"dr_event": {"battery": {"action": "discharge", "to": "home_load"}},
             "price_signal": {"battery": {"action": "discharge", "to": "export", "above_per_kwh": 0.3}}}
    result, _ = _simulate([battery], {}, rules, _context([18], adder=adder), load=2.0)  # idle until then
    assert result.device_dispatch["battery.p_dis"][18] == pytest.approx(5.0)  # the export rule draws less


# --- feeder DR trigger ---------------------------------------------------------------------


def test_loading_trigger_picks_the_days_over_the_threshold():
    grid = TimeGrid(resolution_minutes=60, horizon_steps=24 * 5, start=datetime(2026, 7, 13))
    loading = np.full(24 * 5, 50.0)
    for day, peak in ((0, 95), (2, 120), (3, 85)):
        loading[day * 24 + 17: day * 24 + 20] = peak  # 17:00-20:00 over the trigger
    program = DRProgramConfig(program_id="tx", window="15:00-22:00", min_hours=1, max_hours=4, max_events_per_month=2)
    events = loading_triggered(loading, program, grid, above_pct=90)
    assert [e.day.day for e in events] == [13, 15]  # the two highest; 16 Jul (85%) is under the trigger
    assert all(e.start.hour == 17 and e.hours == 3 for e in events)
    fixed = loading_triggered(loading, program, grid, above_pct=90, duration="fixed", hours=2)
    assert all(e.hours == 2 for e in fixed)


# --- end to end ---------------------------------------------------------------------------


def _house(name, element, devices=(), load=1.5, **extra):
    return {"name": name, "element": element, "devices": list(devices), "external_load": [load] * 24, **extra}


BATTERY = {"type": "battery", "name": "battery", "params": {"capacity_kwh": 13.5, "p_max_charge_kw": 5,
                                                             "p_max_discharge_kw": 5, "soc_init_frac": 0.5,
                                                             "soc_final_min_frac": 0.5, "degradation_cost_per_kwh": 0.01}}
EV = {"type": "ev_v2x", "name": "ev", "params": {
    "capacity_kwh": 60, "p_max_charge_kw": 7.2, "p_max_discharge_kw": 0, "soc_init_frac": 0.6,
    "soc_departure_min_frac": 0.8, "pattern": {"weekday": {"depart": "07:00", "arrive": "17:00", "miles": 40},
                                               "weekend": "home", "kwh_per_mile": 0.3}}}


def _feeder(coordination=None, **changes) -> FeederScenarioConfig:
    load = [1.0] * 17 + [3.0] * 5 + [1.0] * 2
    data = {
        "name": "small", "time_grid": {"resolution_minutes": 60, "horizon_steps": 24, "start": "2026-07-13T00:00:00"},
        "feeder": {"elements": [{"name": "tx", "rating_kw": 15, "overload_cost_per_kwh": 1.0}],
                   "value_streams": [{"type": "capacity_deferral", "element": "tx", "value_per_kw": 10}],
                   "background_load": [{"name": "shop", "element": "tx", "load": [2.0] * 24}]},
        "house_defaults": {"value_streams": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "daily_tou", "hourly_pattern": [0.2] * 16 + [0.5] * 5 + [0.2] * 3},
            "export_price": {"type": "constant", "price": 0.05}}}],
            "poi_limit_kw": 20, "counterfactual": {"policies": {"ev": {"type": "rule_based_ev"},
                                                                "battery": {"type": "self_consumption"}}}},
        "houses": [
            _house("h1", "tx", [BATTERY]) | {"external_load": load},
            _house("h2", "tx", [EV]),
            _house("h3", "tx", [EV], baseline_behavior="rule_based", response="rules"),
            _house("h4", "tx", [EV], participation="non_participant", baseline_behavior="rule_based"),
        ],
    }
    if coordination is not None:
        data["coordination"] = coordination
    for key, value in changes.items():
        data[key] = value
    return FeederScenarioConfig.model_validate(data)


COORD = {
    "price_signal": {"source": "coordinated"},
    "envelopes": {},
    "dr_events": {"element": "tx", "trigger": {"above_pct": 50},
                  "program": {"program_id": "tx", "window": "16:00-22:00", "min_hours": 1, "max_hours": 3,
                              "response": "incentive", "incentive_per_kwh": 0.5}},
}


@pytest.fixture(scope="module")
def study():
    return run_feeder_study(build_feeder_scenario(_feeder(COORD)))


def test_runs_include_each_mechanism_combined_and_the_twin(study):
    assert list(study.runs) == ["coordinated", "independent", "counterfactual", "price_signal", "envelopes",
                                "dr_events", "combined (no events)", "combined"]
    runs = study.coordination.runs
    assert runs["combined"].twin == "combined (no events)" and runs["combined (no events)"].is_twin
    assert runs["dr_events"].twin == "independent"
    assert runs["combined (no events)"].mechanisms == ("price_signal", "envelopes")


def test_background_load_and_non_participants_are_fixed(study):
    scenario = study.scenario
    assert scenario.houses.loc["h4", "group"] == "non-participants"
    h4 = {label: run.dispatches["h4"].grid_import for label, run in study.runs.items() if label != "counterfactual"}
    first = next(iter(h4.values()))
    assert all(series.equals(first) for series in h4.values())  # the same in every run
    independent = study.runs["independent"]
    houses = sum(d.grid_import - d.grid_export for d in independent.dispatches.values())
    assert np.allclose(independent.flows["tx"].to_numpy(), houses.to_numpy() + 2.0)  # + the shop


def test_independent_follows_baseline_behavior_and_rules_respond(study):
    from der_opt.simulation import CounterfactualSimulator

    scenario = study.scenario
    h3 = next(p for p in scenario.premises if p.name == "h3")
    rule = CounterfactualSimulator(h3, {"ev": RuleBasedEVChargingPolicy(7.2, h3.devices[0].soc_max)}).run()
    assert np.allclose(study.runs["independent"].dispatches["h3"].grid_import, rule.grid_import)
    calendar = study.coordination.runs["dr_events"].calendar
    events = pd.to_datetime(calendar["start"])
    during = study.runs["dr_events"].dispatches["h3"].device_dispatch["ev.p_ch"]
    in_event = (during.index >= events.iloc[0]) & (during.index < pd.to_datetime(calendar["end"]).iloc[0])
    assert during[in_event].sum() == 0  # the rule-responding EV pauses for the event


def test_reported_tables(study):
    table = study.coordination_table()
    assert table.loc["coordinated", "network_cost"] <= table.loc["independent", "network_cost"] + 1e-6
    assert pd.notna(table.loc["combined", "share_of_ideal"]) and "signal_charges" in table.columns
    assert {"bills: participants, optimized", "bills: participants, rules", "bills: non-participants"} <= set(table)
    customers = study.customers_table()
    assert set(customers["run"]) == set(study.runs) and len(customers) == 4 * len(study.runs)
    assert not study.checks.errors, study.checks.errors


def test_a_listed_run_is_reused_as_the_twin():
    runs = {"both": ["price_signal", "envelopes"], "all": ["price_signal", "envelopes", "dr_events"]}
    result = run_feeder_study(build_feeder_scenario(_feeder(COORD | {"runs": runs})), counterfactual=False)
    assert list(result.coordination.runs) == ["both", "all"] and result.coordination.runs["all"].twin == "both"


def test_validate_catches_coordination_mistakes():
    bad = _feeder(COORD | {"runs": {"independent": ["envelopes"], "x": ["price_signal"]}},
                  houses=[_house("h1", "tx", [BATTERY], response="rules"),
                          _house("h2", "tx", [EV], baseline_behavior="rule_based", response="rules",
                                 rules={"dr_event": {"ev": {"action": "discharge"}}})])
    found = [f.message for f in check_coordination(build_feeder_scenario(bad)) if f.level == "error"]
    assert any("h1: response: rules keeps the baseline rules" in m for m in found)
    assert any("h2: rules.dr_event.ev: discharge needs a V2X EV" in m for m in found)
    assert any("run name 'independent' is taken" in m for m in found)


def test_cli_run_writes_the_coordination_files(tmp_path):
    import yaml

    path = tmp_path / "small.yaml"
    path.write_text(yaml.safe_dump(_feeder(COORD).model_dump(mode="json", by_alias=True, exclude_none=True)),
                    encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "Coordination runs (each against independent" in result.output and "(the ideal saves $" in result.output
    for name in ("coordination.csv", "customers.csv", "envelopes_envelopes.csv", "signal_price_signal.csv",
                 "dr_calendar_dr_events.csv", "dispatch_combined_no_events.csv"):
        assert (out / name).exists(), name


def test_the_example_shows_each_mechanism_against_the_ideal():
    from pathlib import Path

    from der_opt.scenarios import feeder_from_study, load_study_config

    path = Path(__file__).parent.parent.parent / "examples" / "scenarios" / "feeder_coordination.yaml"
    study = run_feeder_study(feeder_from_study(load_study_config(path)), counterfactual=False)
    table = study.coordination_table()
    assert table.loc["envelopes", "share_of_ideal"] > 0.5 and table.loc["combined", "share_of_ideal"] > 0.9
    assert table.loc["price_signal", "network_saving"] < 0  # the post-window rebound
    assert not study.checks.errors
