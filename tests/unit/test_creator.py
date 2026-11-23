"""The scenario creator (docs/SCENARIO_CREATOR_SPEC.md v1.0): its writer, blocks where
they fit, and its presets writing what `der-opt new`'s templates wrote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from der_opt.creator.presets import PRESETS, preset
from der_opt.creator.writer import write
from der_opt.scenarios.compose import compose_scenario
from der_opt.scenarios.templates import Options, write_template

REPO = Path(__file__).parent.parent.parent
DATA = REPO / "examples" / "data"
LIBRARY = REPO / "examples" / "library"

OPTIONS = Options(timeseries=DATA / "spec_sample" / "annual_home.csv", premise="home_a",
                  tariff=DATA / "tariffs" / "pge_e_elec_2026-06.yaml", export_rates=DATA / "pge_nbt_eec_2026.csv",
                  avoided_costs=DATA / "avoided_costs" / "synthetic_2026",
                  price=DATA / "caiso_lmp_np15_dam_2026-07-13.csv", premises=DATA / "spec_sample" / "premises.csv",
                  devices=DATA / "spec_sample" / "devices.csv", network=DATA / "spec_sample" / "network.csv")


def _resolved(path: Path) -> dict:
    data = compose_scenario(path).data
    data.pop("name", None)
    if "defaults" in data:  # the same entries; order only matters where two set one thing
        data["defaults"] = sorted(data["defaults"], key=lambda d: json.dumps(d, sort_keys=True))
    return json.loads(json.dumps(data, sort_keys=True, default=str))


@pytest.mark.parametrize("name", sorted(PRESETS))
@pytest.mark.parametrize("library", [None, LIBRARY])
def test_each_preset_writes_what_its_template_wrote(tmp_path, name, library):
    old = tmp_path / "old" / f"{name}.yaml"
    write_template(name, old, OPTIONS)
    new = tmp_path / "new" / f"{name}.yaml"
    written = write(preset(name, OPTIONS), new, base=REPO, library=library)
    assert _resolved(new) == _resolved(old)
    if library is not None and name != "feeder-from-files":
        assert written.extends, "a preset reuses the library's blocks where they fit"


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_each_preset_survives_the_page(name):
    # to the form the page holds (YAML boxes, sizes apart from other settings), as JSON, and back
    from der_opt.ui.creator import from_form, to_form

    answers = preset(name, OPTIONS)
    again, problems = from_form(json.loads(json.dumps(to_form(answers))))
    assert not problems
    assert again.model_dump() == answers.model_dump()


def test_rules_and_comparisons_from_selects():
    from der_opt.ui.creator import from_form, to_form

    form = to_form(preset("feeder-from-files", OPTIONS))
    form["participation"].update(response="rules", baseline_behavior="rule_based", rules={
        "dr_event": {"ev": {"action": "pause"}, "battery": {"action": None}}},
        more_rules_yaml="dr_event: {ev: {catch_up: true}}")
    answers, problems = from_form(form)
    assert not problems and answers.participation.rules == {"dr_event": {"ev": {"action": "pause", "catch_up": True}}}
    home = to_form(preset("dr-battery", OPTIONS))
    home["value"]["cost_test"]["without"] = {"battery": "backup_only", "ev": ""}
    assert from_form(home)[0].value.cost_test["without"] == {"battery": "backup_only"}


def test_a_window_from_a_date_a_time_and_days():
    from der_opt.ui.creator import from_form, to_form

    form = to_form(preset("home-bill", OPTIONS))
    form["when"] = {"mode": "window", "date": "2026-07-13", "time": "12:00", "days": 3, "resolution_minutes": "60"}
    answers, problems = from_form(form)
    assert not problems
    assert (answers.when.start, answers.when.steps, answers.when.year) == ("2026-07-13T12:00:00", 72, None)
    form["when"] = {"mode": "window", "date": None, "time": "00:00", "days": 3}
    assert any("start date" in m for _, m in from_form(form)[1])
    form["when"] = {"mode": "year", "year": 2026, "date": "2026-07-13", "days": 3}
    answers, _ = from_form(form)
    assert (answers.when.year, answers.when.start) == (2026, None)
