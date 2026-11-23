"""What each device is compared against (docs/COST_TEST_SPEC.md v1.3): modes and their
defaults, the TOU-timer EV, and the solved backup-only and events-only comparison homes."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from der_opt.cost_test.without import (
    WithoutError,
    build_without,
    config_modes,
    device_modes,
    label,
)
from der_opt.dr import run_dr_study
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.loader import build_counterfactual_policies, build_premise
from der_opt.scenarios.schema import ScenarioConfig
from der_opt.simulation import CounterfactualSimulator

REPO = Path(__file__).parent.parent.parent
LIBRARY = REPO / "examples" / "library"
HOME = REPO / "examples" / "data" / "spec_sample" / "annual_home.csv"


def _home(tmp_path: Path, devices: list[str], without: dict | None = None, **changes) -> ScenarioConfig:
    """A July week of the sample home on E-ELEC (no export credit) with a DR program
    paying uniformly over 4-hour events, and the named library devices."""
    blocks = {"battery": "devices/battery_13kwh.yaml", "ev": "devices/ev_commuter_v2x.yaml"}
    scenario = {
        "extends": [str(LIBRARY / blocks[d]) for d in devices] + [str(LIBRARY / "value_streams/bill.yaml"),
                                                                  str(LIBRARY / "meter/panel_20kw.yaml")],
        "name": "week",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 168, "start": "2026-07-13T00:00:00"},
        "external_load": {"source": "timeseries", "path": str(HOME), "premise_id": "home_a", "column": "load_kw"},
        "billing": {"tariff": str(REPO / "examples/data/tariffs/pge_e_elec_2026-06.yaml")},
        "counterfactual": {"policies": {d: {"type": "self_consumption" if d == "battery" else "tou_timer"}
                                        for d in devices}},
        "dr": {"customer_class": "residential", "shadow_events": False,
               "program": {"program_id": "p", "season": [7], "max_hours": 4, "incentive_per_kwh": 1.0,
                           "delivery": "uniform"},
               "events": {"schedule": [{"start": "16:00", "hours": 4, "dates": ["2026-07-16"]}]}},
    }
    if without is not None:
        scenario["cost_test"] = {"start_year": 2027, "measure_life_years": 10, "without": without,
                                 "avoided_costs": {"set": str(REPO / "examples/data/avoided_costs/synthetic_2026")}}
    scenario.update(changes)
    path = tmp_path / "week.yaml"
    path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return ScenarioConfig.model_validate(compose_scenario(path).data)


def test_modes_defaults_and_mistakes():
    devices = [("battery", "battery"), ("ev", "ev"), ("ac", "thermostat"), ("pv", "solar")]
    modes, unmatched = device_modes(devices, {"battery": "backup_only", "heat_pump": "removed"})
    assert {n: m["mode"] for n, m in modes.items()} == {"battery": "backup_only", "ev": "tou_timer",
                                                        "ac": "rule_based", "pv": "unchanged"}
    assert unmatched == ["heat_pump"]
    with pytest.raises(WithoutError, match="needs a mode for battery"):  # a battery has no default
        device_modes(devices, {})
    with pytest.raises(WithoutError, match="can't be 'removed'"):
        device_modes(devices, {"battery": "removed", "thermostat": "removed"})
    timed, _ = device_modes([("ev", "ev")], {"ev": {"mode": "tou_timer", "start": "21:00"}})
    assert timed["ev"] == {"mode": "tou_timer", "start": "21:00"}


def test_a_tou_timer_ev_waits_for_the_cheapest_period(tmp_path):
    config = _home(tmp_path, ["ev"])
    premise = build_premise(config)
    result = CounterfactualSimulator(premise, build_counterfactual_policies(config, premise)).run()
    charge = result.device_dispatch["ev.p_ch"]
    hours = pd.DatetimeIndex(charge.index).hour
    assert charge[charge > 1e-6].index.size > 0
    assert set(hours[charge.to_numpy() > 1e-6]) <= set(range(15))  # E-ELEC's off-peak: midnight to 3 pm
    assert result.device_dispatch["ev.p_dis"].max() == pytest.approx(0)


def test_a_backup_only_battery_stays_full_and_discharges_only_into_events(tmp_path):
    config = _home(tmp_path, ["battery"], without={"battery": "backup_only"})
    study = run_dr_study(config)
    result, pricing, incentive = build_without(config, study)
    soc = result.device_dispatch["battery.soc"]
    net_power = result.device_dispatch["battery.net_power"]
    event = (soc.index >= pd.Timestamp("2026-07-16 16:00")) & (soc.index < pd.Timestamp("2026-07-16 20:00"))
    assert net_power[event].min() > 0.5  # it discharges into the event, every hour of it
    after_charge = soc.index >= pd.Timestamp("2026-07-13 06:00")
    quiet = after_charge & (soc.index < pd.Timestamp("2026-07-16 16:00"))
    assert soc[quiet].min() == pytest.approx(pricing.devices[0].soc_max, abs=1e-3)  # full until the event
    assert net_power[~event & after_charge].max() < 1e-6  # and never discharges outside it
    assert incentive is not None and incentive > 0
    assert label(config, config_modes(config))[0] == "Battery backup-only"


def test_an_events_only_ev_responds_to_the_event_and_otherwise_keeps_its_timer(tmp_path):
    config = _home(tmp_path, ["ev"], without={"ev": "events_only"})
    study = run_dr_study(config)
    result, _, incentive = build_without(config, study)
    frame = result.device_dispatch
    hours = pd.DatetimeIndex(frame.index).hour
    event_day = pd.DatetimeIndex(frame.index).normalize() == pd.Timestamp("2026-07-16")
    charging = frame["ev.p_ch"].to_numpy() > 1e-6
    assert set(hours[charging & ~event_day]) <= set(range(15))  # on its timer away from the event
    assert frame["ev.p_dis"][~event_day].max() == pytest.approx(0, abs=1e-6)  # no TOU arbitrage
    assert np.isfinite(incentive)
