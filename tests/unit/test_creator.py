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
