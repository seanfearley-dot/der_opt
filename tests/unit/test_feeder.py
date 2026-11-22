"""Feeder scenarios from YAML, and the feeder CLI run."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.scenarios import build_feeder_scenario, run_feeder_study
from der_opt.scenarios.schema import FeederScenarioConfig

REPO = Path(__file__).parent.parent.parent
EXAMPLE = REPO / "examples" / "scenarios" / "feeder_ev_heat_wave.yaml"


def _ev_home(name: str, arrive: str) -> dict:
    """A home with an EV on a commuting pattern, arriving at `arrive`."""
    return {"name": name, "element": "tx", "external_load": [1.2] * 48, "devices": [{
        "type": "ev_v2x", "name": "ev", "params": {
            "capacity_kwh": 60, "p_max_charge_kw": 7.2, "p_max_discharge_kw": 0, "eta_charge": 0.92,
            "eta_discharge": 0.92, "soc_max_frac": 0.9, "soc_init_frac": 0.7, "soc_departure_min_frac": 0.9,
            "pattern": {"weekday": {"depart": "07:00", "arrive": arrive, "miles": 35}, "weekend": "home",
                        "kwh_per_mile": 0.3}}}]}


def _small_feeder(tmp_path, rating=None):
    config = {
        "name": "small",
        "time_grid": {"resolution_minutes": 60, "horizon_steps": 48, "start": "2026-07-13T12:00:00"},
        "feeder": {"elements": [{"name": "tx", "rating_kw": rating}]},
        "houses": [{"name": "plain", "element": "tx", "external_load": [1.0] * 48},
                   *[_ev_home(f"ev{i}", arrive) for i, arrive in enumerate(("17:00", "17:30", "18:00", "19:00"), 1)]],
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
    assert list(scenario.houses.index) == ["plain", "ev1", "ev2", "ev3", "ev4"]
    assert scenario.houses.loc["ev1", "devices"] == "ev"
    by_name = {c.name: c for c in scenario.house_configs}
    assert by_name["plain"].counterfactual.policies == {}  # no ev, so no ev policy
    assert set(by_name["ev1"].counterfactual.policies) == {"ev"}


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
