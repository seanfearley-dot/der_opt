"""DR events and capacity (docs/DR_CAPACITY_SPEC.md v1.0): calendar rules and
constructors, settlement baselines, responses, references, counting and accounting."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.dr import calendar as cal
from der_opt.dr import run_dr_study, settlement_baselines
from der_opt.dr.calendar import CalendarError, Event
from der_opt.scenarios.schema import (
    DRPriceEventsConfig,
    DRProgramConfig,
    DRRandomEventsConfig,
    DRScheduleRule,
    ScenarioConfig,
)
from der_opt.time_grid import TimeGrid

JULY = TimeGrid(60, 31 * 24, datetime(2026, 7, 1))  # July 4 2026 is a Saturday, observed Friday the 3rd


def _program(**kw):
    base = {"program_id": "p", "season": [7], "window": "16:00-21:00", "min_hours": 1, "max_hours": 4,
            "incentive_per_kwh": 1.0}
    return DRProgramConfig(**(base | kw))


def _event(day, start_h, hours, **kw):
    start = pd.Timestamp(datetime(2026, 7, day, start_h))
    return Event(kw.get("event_id", f"e{day}"), kw.get("program_id", "p"), kw.get("event_type", "dispatch"),
                 start, start + pd.Timedelta(hours=hours))


# --- calendar rules -----------------------------------------------------------------


@pytest.mark.parametrize("event, message", [
    (_event(11, 17, 2), "weekend"),  # a Saturday
    (_event(3, 17, 2), "holiday"),  # Independence Day, observed
    (_event(7, 14, 2), "window"),
    (_event(7, 16, 5), "outside 1-4 h"),
    (Event("x", "p", "dispatch", pd.Timestamp("2026-07-07 16:30"), pd.Timestamp("2026-07-07 18:30")), "steps"),
    (Event("x", "p", "maybe", pd.Timestamp("2026-07-07 16:00"), pd.Timestamp("2026-07-07 18:00")), "dispatch or test"),
])
def test_calendar_rules(event, message):
    assert any(message in f.message for f in cal.validate_calendar([event], _program(), JULY))


def test_one_event_a_day():
    events = [_event(7, 16, 1), _event(7, 19, 1, event_id="later")]  # same day, no overlap
    assert any("2 events on 2026-07-07" in f.message for f in cal.validate_calendar(events, _program(), JULY))
    assert not cal.validate_calendar([_event(7, 16, 1), _event(8, 16, 1, event_id="next")], _program(), JULY)


def test_calendar_limits_and_overlap():
    p = _program(max_events_per_month=2, max_consecutive_days=2)
    events = [_event(7, 16, 2), _event(8, 16, 2, event_id="b"), _event(9, 16, 2, event_id="c"),
              _event(9, 17, 1, event_id="d")]
    messages = " | ".join(f.message for f in cal.validate_calendar(events, p, JULY))
    assert "overlap" in messages and "over the limit of 2" in messages and "consecutive days" in messages
    with pytest.raises(CalendarError):
        cal.check_calendar(events, p, JULY)


def test_events_file_converts_offsets_to_the_local_clock(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text("event_id,start,end,program_id,event_type\n"
                    "a,2026-07-07T23:00:00Z,2026-07-08T01:00:00Z,p,test\n", encoding="utf-8")
    [event] = cal.from_file(path, _program(), JULY, "America/Los_Angeles")
    assert event.start == pd.Timestamp("2026-07-07 16:00") and event.event_type == "test"
    path.write_text("event_id,start,end,program_id,event_type\na,2026-07-07T16:00:00,2026-07-07T18:00:00,p,test\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="UTC offset"):
        cal.from_file(path, _program(), JULY, "America/Los_Angeles")


# --- constructors -------------------------------------------------------------------


def _price(peaks: dict[int, float]):
    """$0.10 everywhere, except a 3-hour shape 17-20 h on the given days."""
    stamps = pd.DatetimeIndex(JULY.timestamps())
    price = np.full(len(stamps), 0.10)
    for day, height in peaks.items():
        for h, share in ((17, 0.9), (18, 1.0), (19, 0.85), (20, 0.5)):
            price[(stamps.day == day) & (stamps.hour == h)] = height * share
    return price


def test_price_based_fixed_and_variable():
    price = _price({7: 1.0, 8: 2.0, 9: 0.5, 14: 3.0})
    fixed = cal.price_based(price, _program(), JULY, DRPriceEventsConfig(series={}, events_per_year=2, hours=2))
    assert [e.day for e in fixed] == [date(2026, 7, 8), date(2026, 7, 14)]
    assert all(e.start.hour == 17 and e.hours == 2 for e in fixed)  # the best 2-hour block, 17-19
    variable = cal.price_based(price, _program(), JULY, DRPriceEventsConfig(series={}, events_per_year=1,
                                                                             duration="variable", alpha=0.8))
    [e] = variable
    assert e.day == date(2026, 7, 14) and (e.start.hour, e.hours) == (17, 3)  # 0.9, 1.0, 0.85 >= 0.8; 0.5 isn't


def test_randomized_is_reproducible_and_obeys_the_program():
    p = _program(max_consecutive_days=1)
    settings = DRRandomEventsConfig(events_per_month=5, seed=3, min_days_between=1, hours=None)
    a, b = cal.randomized(p, JULY, settings, 0), cal.randomized(p, JULY, settings, 0)
    assert [(e.start, e.hours) for e in a] == [(e.start, e.hours) for e in b] and len(a) == 5
    assert cal.randomized(p, JULY, settings, 1) != a  # another replicate, another draw
    days = sorted(e.day for e in a)
    assert all((y - x).days > 1 for x, y in zip(days, days[1:], strict=False))  # spacing respected
    heavy = DRRandomEventsConfig(events_per_month=1, seed=0, weighting="price", weight_power=50, hours=2)
    assert cal.randomized(_program(), JULY, heavy, 0, _price({21: 5.0}))[0].day == date(2026, 7, 21)


def test_schedule_rules():
    rules = [DRScheduleRule(start="17:00", hours=2, days="weekdays", months=[7]),
             DRScheduleRule(start="16:00", hours=1, nth_weekday={"n": 1, "weekday": "tue"}, event_type="test")]
    with pytest.raises(CalendarError, match="already uses"):
        cal.schedule(rules, _program(), JULY)  # the first Tuesday is already a weekday event
    tests = cal.schedule(rules[1:], _program(), JULY)
    assert [(e.day, e.event_type) for e in tests] == [(date(2026, 7, 7), "test")]
    weekdays = cal.schedule(rules[:1], _program(), JULY)
    assert date(2026, 7, 3) not in {e.day for e in weekdays}  # the observed holiday is skipped
    assert len(weekdays) == 22


# --- settlement ---------------------------------------------------------------------


def _net(levels: dict[date, float], default=1.0):
    stamps = pd.DatetimeIndex(JULY.timestamps())
    values = np.array([levels.get(t.date(), default) for t in stamps])
    return pd.Series(values, index=stamps)


def test_residential_5_in_10_and_commercial_10_in_10():
    weekdays = [d for d in sorted(set(pd.DatetimeIndex(JULY.timestamps()).date)) if cal.day_type(d) == "weekday"]
    prior = [d for d in weekdays if d < date(2026, 7, 21)][-10:]
    levels = {d: float(i + 1) for i, d in enumerate(prior)}  # 1..10 kW, flat all day
    levels[date(2026, 7, 21)] = 0.5
    event = _event(21, 17, 2)
    res = settlement_baselines(_net(levels), [event], "residential")[event.event_id]
    com = settlement_baselines(_net(levels), [event], "commercial")[event.event_id]
    assert sorted(res.days_used) == sorted(prior[-5:])
    # flat days: the adjustment window looks like the event hours, so the ratio is
    # (0.5 / 8) on the 5 highest days, capped at -20%
    assert res.adjustment == pytest.approx(0.8) and res.values == pytest.approx([8.0 * 0.8] * 2)
    assert com.values == pytest.approx([5.5 * 0.8] * 2)


def test_event_days_are_excluded_and_early_events_have_no_baseline():
    events = [_event(1, 17, 2, event_id="first"), _event(2, 17, 2, event_id="second")]
    out = settlement_baselines(_net({}), events, "residential")
    assert out["first"].note == "no eligible baseline days"
    assert date(2026, 7, 1) not in out["second"].days_used


# --- the study ----------------------------------------------------------------------


def _config(days=31, devices=None, dr=None, **extra):
    steps = days * 24
    hours = np.arange(steps) % 24
    load = (1.0 + 1.5 * ((hours >= 17) & (hours < 22))).tolist()
    raw = {
        "name": "dr_test", "time_grid": {"resolution_minutes": 60, "horizon_steps": steps, "start": "2026-07-01T00:00:00"},
        "devices": devices if devices is not None else [{"type": "battery", "name": "battery", "params": {
            "capacity_kwh": 12, "p_max_charge_kw": 5, "p_max_discharge_kw": 5, "eta_charge": 1.0, "eta_discharge": 1.0,
            "soc_init_frac": 0.5, "soc_final_min_frac": 0.5,
            # a tiny cycling cost makes idle the only optimum for R2 under flat prices
            "degradation_cost_per_kwh": 0.001}}],
        "value_streams": [{"type": "energy_arbitrage", "params": {
            "import_price": {"type": "constant", "price": 0.30}, "export_price": {"type": "constant", "price": 0.0}}}],
        "external_load": load, "poi_limit_kw": 20,
        "dr": dr or {"program": {"program_id": "p", "season": [7], "max_hours": 3, "incentive_per_kwh": 1.0},
                     "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-21", "2026-07-28"]},
                                             {"start": "17:00", "hours": 3, "dates": ["2026-07-14"], "event_type": "test"}]}},
    }
    return ScenarioConfig.model_validate(raw | extra)


def test_study_references_and_capacity():
    study = run_dr_study(_config())
    perf = study.performance.set_index("event_id")
    # flat prices: R2 leaves the battery idle; on an event the 12 kWh battery covers the
    # 2.5 kW evening load for 3 h (7.5 kWh) and exports the rest (4.5 kWh) at 5 kW cap:
    # net = 2.5 - 5 = -2.5 kW at best; it has 6 kWh at 17:00 plus what it charges first
    assert perf["R2_kw"].tolist() == pytest.approx([2.5] * 3)
    assert perf["R3_kw"].tolist() == pytest.approx([2.5] * 3)
    assert perf["reduction_vs_R2_kw"].tolist() == pytest.approx([4.0] * 3)  # 12 kWh over 3 h
    assert set(perf["event_type"]) == {"dispatch", "test"}  # test events are accounted
    cap = study.capacity.set_index("month")
    assert cap.loc[7, "events"] == 3 and cap.loc[7, "test_events"] == 1
    assert cap.loc[7, "accounted_R2_kw"] == pytest.approx(4.0)
    # import basis: the 2.5 kW evening load goes to zero import; the other 1.5 kW is export
    assert perf["import_reduction_vs_R2_kw"].tolist() == pytest.approx([2.5] * 3)
    assert cap.loc[7, "accounted_import_R2_kw"] == pytest.approx(2.5)
    assert cap.loc[7, "counted_import_R2_kw"] == pytest.approx(2.5)
    # counted: every eligible July weekday, from R2's state (6 kWh) with time to charge
    assert cap.loc[7, "shadow_days"] == 22 and cap.loc[7, "counted_R2_kw"] == pytest.approx(4.0)
    assert pd.isna(cap.loc[8, "counted_R2_kw"]) if "counted_R2_kw" in cap else True
    assert study.summary["incentive_earned"] > 0
    assert study.checks.errors == []


def test_counted_capacity_covers_months_without_events():
    dr = {"program": {"program_id": "p", "season": [7, 8], "max_hours": 2, "incentive_per_kwh": 1.0},
          "events": {"schedule": [{"start": "17:00", "hours": 2, "dates": ["2026-07-21"]}]}, "capacity_reference": "R2"}
    dr["program"]["capacity_reference"] = "R2"
    del dr["capacity_reference"]
    study = run_dr_study(_config(days=62, dr=dr))
    cap = study.capacity.set_index("month")
    assert cap.loc[8, "events"] == 0 and cap.loc[8, "counted_R2_kw"] > 0  # August: counted, never called
    assert "accounted_R3_kw" not in cap and "counted_S_kw" not in cap  # only the chosen reference
    sod = study.slice_of_day
    assert set(sod.loc[sod["month"] == 8, "source"]) == {"counted"} and set(sod.loc[sod["month"] == 7, "source"]) == {"accounted"}


def test_r3_removes_only_the_measure_devices():
    devices = [{"type": "solar", "name": "solar", "params": {"available_kw": [0.0] * 31 * 24}},
               {"type": "battery", "name": "battery", "params": {"capacity_kwh": 10, "p_max_charge_kw": 5,
                                                                 "p_max_discharge_kw": 5}}]
    from der_opt.dr.study import _without_measure

    config = _config(devices=devices)
    config.dr.measure_devices = ["battery"]
    assert [d.name for d in _without_measure(config).devices] == ["solar"]
    config.dr.measure_devices = ["heat_pump"]
    with pytest.raises(ValueError, match="aren't devices"):
        _without_measure(config)


def test_commitment_and_import_cap_responses():
    for response, extra in (("commitment", {"committed_kw": 10.0}), ("import_cap", {"cap_kw": 0.5})):
        dr = {"program": {"program_id": "p", "season": [7], "max_hours": 3, "response": response} | extra,
              "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-21"]}]}, "shadow_events": False}
        study = run_dr_study(_config(dr=dr))
        row = study.performance.iloc[0]
        assert row["response"] == response
        if response == "commitment":  # 10 kW below baseline is more than a 5 kW battery can do
            assert row["shortfall_kwh"] > 0 and any(f.check == "dr shortfall" for f in study.checks.findings)
        else:  # the 2.5 kW evening load fits under a 0.5 kW import cap with 2 kW from the battery
            assert row["shortfall_kwh"] == pytest.approx(0, abs=1e-6)


def test_cli_dr_outputs(tmp_path):
    import yaml

    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(json.loads(_config().model_dump_json(exclude_none=True))), encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["run", str(path), "-o", str(out)])
    assert result.exit_code == 0, result.stdout
    assert "Demand response: program p (response incentive" in result.stdout and "non-responding twin" in result.stdout
    for name in ("dr_events.csv", "dr_event_performance.csv", "dr_capacity.csv", "dr_capacity_slice_of_day.csv",
                 "dr_net_load_kw.csv"):
        assert (out / name).exists(), name
    record = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    assert record["results"]["dr"]["response"] == "incentive"


def test_shadow_events_slice_evs_and_thermostats():
    from der_opt.dr.shadow import shadow_event
    from der_opt.scenarios import build_premise

    steps = 4 * 24
    hours = np.arange(steps) % 24
    connected = ((hours >= 18) | (hours < 7)).astype(float).tolist()
    devices = [
        {"type": "ev_v2x", "name": "ev", "params": {
            "capacity_kwh": 60, "p_max_charge_kw": 7.2, "p_max_discharge_kw": 7.2, "connected": connected,
            "soc_init_frac": 0.5, "departure_period": [31, 55, 79], "soc_departure_min_frac": 0.8,
            "degradation_cost_per_kwh": 0.01}},
        {"type": "thermostat", "name": "ac", "params": {
            "thermal_resistance_r": 2.0, "thermal_capacitance_c": 5.0, "ambient_temp_c": [32.0] * steps,
            "comfort_min_c": 20.0, "comfort_max_c": 25.0, "cop": 3.0, "p_max_kw": 4.0, "initial_temp_c": 23.0}},
    ]
    config = _config(days=4, devices=devices)
    premise = build_premise(config)
    r2, _, _ = premise.solve()
    day = 2 * 24
    # a 19-22 h block: the EV is home, discharges to cut load, and must still leave at 80% next morning
    # (that departure is outside this horizon, so only the block's own limits apply here)
    shadow = shadow_event(premise, r2, day, day + 19, 3)
    r2_net = (r2.grid_import - r2.grid_export).iloc[day + 19: day + 22].to_numpy()
    assert (r2_net - shadow).mean() > 5  # V2X at 7.2 kW plus pre-cooling
    assert len(shadow) == 3


def test_shadow_events_cover_load_before_exporting():
    # 6 kWh over a 3 h block with a 2.5 kW load: all of it should go to the load (2 kW
    # less import every hour), not to exports in one hour and nothing in another
    devices = [{"type": "battery", "name": "battery", "params": {
        "capacity_kwh": 6, "p_max_charge_kw": 5, "p_max_discharge_kw": 5, "eta_charge": 1.0, "eta_discharge": 1.0,
        "soc_init_frac": 0.5, "soc_final_min_frac": 0.5, "degradation_cost_per_kwh": 0.001}}]
    study = run_dr_study(_config(devices=devices))
    cap = study.capacity.set_index("month")
    assert cap.loc[7, "counted_R2_kw"] == pytest.approx(2.0)
    assert cap.loc[7, "counted_import_R2_kw"] == pytest.approx(2.0)
    assert set(study.slice_of_day["basis"]) == {"net", "import"}


def test_settlement_on_the_import_basis_stops_at_zero_import():
    dr = {"program": {"program_id": "p", "season": [7], "max_hours": 3, "incentive_per_kwh": 1.0,
                      "settlement_basis": "import"},
          "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-21", "2026-07-28"]}]},
          "shadow_events": False}
    study = run_dr_study(_config(dr=dr))
    perf = study.performance
    # paid only for avoided import, the battery covers the 2.5 kW load and exports nothing
    assert perf["reduction_vs_R2_kw"].tolist() == pytest.approx([2.5, 2.5], abs=1e-6)
    assert perf["import_reduction_vs_R2_kw"].tolist() == pytest.approx([2.5, 2.5], abs=1e-6)
    assert set(perf["settlement_basis"]) == {"import"} and study.summary["settlement_basis"] == "import"
    # at least 2.5 kW x 3 h x $1; a day-of adjustment lifted by pre-event charging can add more
    assert (perf["incentive_earned"] >= 7.5 - 1e-6).all()
    assert study.checks.errors == []


def test_net_basis_settles_on_net_load():
    # regression: the both-bases reporting loop once overwrote the program's basis
    study = run_dr_study(_config(dr={"program": {"program_id": "p", "season": [7], "max_hours": 3,
                                                 "incentive_per_kwh": 1.0},
                                     "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-21"]}]},
                                     "shadow_events": False}))
    row = study.performance.iloc[0]
    assert row["settlement_basis"] == "net"
    # paid on net: the full 4 kW reduction below the 2.5 kW net baseline, 3 h, $1
    assert row["incentive_earned"] == pytest.approx(row["reduction_vs_S_kw"] * 3 * 1.0)
    assert row["incentive_earned"] > row["import_reduction_vs_S_kw"] * 3 * 1.0


def _rising_export_config(delivery):
    """A 6 kWh battery, a 3 h event, and an export credit that rises through the event
    (none at 17:00, $0.50 from 18:00): paid per kWh, energy goes to the later hours."""
    export = [0.0] * 24
    export[18] = export[19] = 0.5
    devices = [{"type": "battery", "name": "battery", "params": {
        "capacity_kwh": 6, "p_max_charge_kw": 5, "p_max_discharge_kw": 5, "eta_charge": 1.0, "eta_discharge": 1.0,
        "soc_init_frac": 0.5, "soc_final_min_frac": 0.5, "degradation_cost_per_kwh": 0.001}}]
    dr = {"program": {"program_id": "p", "season": [7], "max_hours": 3, "incentive_per_kwh": 1.0,
                      "delivery": delivery},
          "events": {"schedule": [{"start": "17:00", "hours": 3, "dates": ["2026-07-21", "2026-07-28"]}]}}
    config = _config(devices=devices, dr=dr)
    config.value_streams[0].params["export_price"] = {"type": "daily_tou", "hourly_pattern": export}
    return config


def test_uniform_delivery_holds_the_reduction_level():
    per_kwh = run_dr_study(_rising_export_config("per_kwh"))
    uniform = run_dr_study(_rising_export_config("uniform"))
    a, b = per_kwh.performance, uniform.performance
    # per kWh: an hour carries far less than the event's mean, and the warning says so
    assert (a["held_vs_S_kw"] < 0.25 * a["reduction_vs_S_kw"]).all()
    assert any(f.check == "dr skipped hours" for f in per_kwh.checks.findings)
    # uniform: every hour the same reduction below S (the held kW is the mean), no warning
    assert b["held_vs_S_kw"].tolist() == pytest.approx(b["reduction_vs_S_kw"].tolist(), abs=1e-3)
    assert (b["held_vs_S_kw"] > 0.25).all()  # the no-events run already exports at $0.50, so 0.5 kW is what's left
    assert not any(f.check == "dr skipped hours" for f in uniform.checks.findings)
    # settled on the held kW x the event's hours
    assert b["incentive_earned"].tolist() == pytest.approx((b["held_vs_S_kw"] * 3 * 1.0).tolist(), abs=1e-6)
    assert uniform.summary["delivery"] == "uniform"
    # capacity under uniform is held kW: counted is a shadow event's weakest hour, accounted the events' held kW
    cap = uniform.capacity.set_index("month")
    assert cap.loc[7, "accounted_S_kw"] == pytest.approx(b["held_vs_S_kw"].mean(), abs=1e-6)
    assert cap.loc[7, "counted_R2_kw"] > 0
