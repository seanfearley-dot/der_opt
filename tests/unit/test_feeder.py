"""Synthetic houses, feeder scenarios from YAML, and the feeder CLI run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.devices import V2XVehicle
from der_opt.network.synthetic import heat_wave_temperature, synthetic_houses
from der_opt.premise import Premise
from der_opt.scenarios import build_feeder_scenario, run_feeder_study
from der_opt.scenarios.schema import FeederScenarioConfig
from der_opt.simulation import CounterfactualSimulator, RuleBasedEVChargingPolicy
from der_opt.time_grid import TimeGrid

REPO = Path(__file__).parent.parent.parent
EXAMPLE = REPO / "examples" / "scenarios" / "feeder_ev_heat_wave.yaml"
NOON_3_DAYS = TimeGrid(resolution_minutes=60, horizon_steps=72, start=datetime(2026, 7, 13, 12))


def test_synthetic_houses_are_reproducible_and_meet_the_shares():
    a = synthetic_houses(NOON_3_DAYS, 10, seed=3, solar_share=0.4, battery_share=0.5, ev_share=0.6)
    b = synthetic_houses(NOON_3_DAYS, 10, seed=3, solar_share=0.4, battery_share=0.5, ev_share=0.6)
    assert [h.load_kw.tolist() for h in a] == [h.load_kw.tolist() for h in b]
    assert sum(h.solar is not None for h in a) == 4
    assert sum(h.battery is not None for h in a) == 2
    assert all(h.solar is not None for h in a if h.battery is not None)
    assert sum(h.ev is not None for h in a) == 6
    assert all(h.load_kw.min() > 0 for h in a)


def test_temperature_peaks_mid_afternoon():
    temps = heat_wave_temperature(TimeGrid(60, 24, datetime(2026, 7, 13)), 20, 40)
    assert temps.argmax() in (15, 16) and temps.argmin() in (5, 6)
    assert temps.max() == pytest.approx(40, abs=0.5)


def _ev_house():
    return next(h for h in synthetic_houses(NOON_3_DAYS, 10, seed=0, ev_share=1.0) if h.ev)


def test_each_full_day_away_uses_the_daily_trip_energy():
    ev = _ev_house().ev
    connected = np.array(ev["connected"])
    driving = np.array(ev["driving_kwh"])
    assert driving[connected == 1].sum() == 0
    # an away block not cut off by the start or end of the horizon uses exactly one trip
    blocks = np.split(np.arange(72), np.where(np.diff(connected) != 0)[0] + 1)
    full = [blk for blk in blocks if connected[blk[0]] == 0 and blk[0] > 0 and blk[-1] < 71]
    assert len(full) == 2
    for blk in full:
        assert driving[blk].sum() == pytest.approx(_ev_house().traits["ev_daily_kwh"], abs=0.05)  # traits round to 0.1


def test_plug_in_charging_and_the_optimizer_both_meet_every_departure():
    ev_params = _ev_house().ev
    ev = V2XVehicle("ev", NOON_3_DAYS, **ev_params)
    premise = Premise("h", NOON_3_DAYS, devices=[ev], external_load=np.ones(72), poi_limit_kw=20)
    simulator = CounterfactualSimulator(premise, {"ev": RuleBasedEVChargingPolicy(ev.p_max_charge_kw, ev.soc_max)})
    soc = simulator.run().device_dispatch["ev.soc"].to_numpy()
    for departure in ev.departure_periods:
        assert soc[departure] == pytest.approx(ev.soc_max, rel=1e-6)
    assert min(soc) > 0


def _small_feeder(tmp_path, rating=None):
    config = {
        "name": "small",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 48, "start": "2026-07-13T12:00:00"},
        "feeder": {"elements": [{"name": "tx", "rating_kw": rating}]},
        "houses": [{"name": "plain", "element": "tx", "external_load": [1.0] * 48}],
        "synthetic_houses": [{"element": "tx", "count": 4, "seed": 5, "ev_share": 1.0, "solar_share": 0.0}],
        "house_defaults": {
            "value_streams": [{"type": "energy_arbitrage", "params": {
                "import_price": {"type": "daily_tou", "hourly_pattern": [0.2] * 16 + [0.5] * 5 + [0.3] * 3},
                "export_price": {"type": "constant", "price": 0.0},
            }}],
            "poi_limit_kw": 30,
            "counterfactual": {"policies": {"ev": {"type": "rule_based_ev"}, "battery": {"type": "self_consumption"}}},
        },
    }
    return build_feeder_scenario(FeederScenarioConfig.model_validate(config))


def test_feeder_scenario_builds_houses_and_applies_defaults(tmp_path):
    scenario = _small_feeder(tmp_path)
    assert list(scenario.houses.index) == ["plain", "tx_h01", "tx_h02", "tx_h03", "tx_h04"]
    assert scenario.houses.loc["tx_h01", "devices"] == "ev"
    by_name = {c.name: c for c in scenario.house_configs}
    assert by_name["plain"].counterfactual.policies == {}  # no ev, so no ev policy
    assert set(by_name["tx_h01"].counterfactual.policies) == {"ev"}


def test_a_hard_rating_holds_in_the_coordinated_run_only():
    loose = run_feeder_study(_small_feeder(None))
    peak_alone = loose.runs["independent"].flows["tx"].max()
    rating = round(peak_alone * 0.7, 1)
    study = run_feeder_study(_small_feeder(None, rating=rating))
    assert study.runs["coordinated"].flows["tx"].max() <= rating + 1e-6
    assert study.runs["independent"].flows["tx"].max() == pytest.approx(peak_alone)
    assert study.runs["coordinated"].total >= study.runs["independent"].house_costs.sum().sum() - 1e-6
    # the limit binds, but charging can move to other equally cheap off-peak hours, so
    # it costs (almost) nothing: prices stay at the tie-break's scale
    assert study.congestion_prices["tx"].abs().max() < 1e-3


def test_cli_runs_the_feeder_example(tmp_path):
    result = CliRunner().invoke(app, ["run", str(EXAMPLE), "-o", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    for text in ("20 houses on 3 network elements", "Peak loading:", "Congestion prices", "Coordination vs each house"):
        assert text in result.stdout
    for name in ("houses.csv", "flows_kw.csv", "loading.csv", "congestion_prices.csv", "dispatch_coordinated.csv", "feeder_loading.png"):
        assert (tmp_path / name).exists(), name
    assert yaml.safe_load(EXAMPLE.read_text())["feeder"]  # it really is a feeder scenario
