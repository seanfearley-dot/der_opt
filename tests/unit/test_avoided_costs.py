"""Avoided cost sets (docs/AVOIDED_COST_SPEC.md v1.0), first-year screening values, and
the measure impact package (docs/COST_TEST_SPEC.md v1.0)."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from der_opt.avoided_costs import SetError, read_set
from der_opt.avoided_costs.screening import screening_values
from der_opt.cli.main import app
from der_opt.cost_test import energy_impact, standard_time_hours
from der_opt.results.schema import DispatchResult
from der_opt.time_grid import TimeGrid

REPO = Path(__file__).parent.parent.parent
EXAMPLE = REPO / "examples" / "data" / "avoided_costs" / "synthetic_2026"
TZ = "America/Los_Angeles"


def _small_set(tmp_path, energy=0.10, **manifest_changes) -> Path:
    """One year (2026): energy $0.10/kWh flat, a generation capacity component, and
    $100/kW-year generation capacity all in July."""
    folder = tmp_path / "set"
    folder.mkdir()
    utc = pd.date_range(pd.Timestamp("2026-01-01", tz=TZ).tz_convert("UTC"),
                        pd.Timestamp("2027-01-01", tz=TZ).tz_convert("UTC"), freq="h", inclusive="left")
    pd.DataFrame({"timestamp": [t.isoformat() for t in utc.tz_convert(TZ)], "energy": energy,
                  "generation_capacity": 0.01, "emissions": 0.4}).to_csv(folder / "hourly.csv", index=False)
    pd.DataFrame([{"year": 2026, "capacity_component": "generation", "price_per_kw_year": 100.0,
                   **{f"m{m}": 1.0 if m == 7 else 0.0 for m in range(1, 13)}}]).to_csv(folder / "capacity.csv", index=False)
    manifest = {"set_id": "small", "source": "test", "dollars": {"basis": "nominal", "base_year": 2026},
                "years": [2026, 2026], "emissions": "emissions",
                "components": {"energy": {"class": "energy"},
                               "generation_capacity": {"class": "capacity", "capacity_component": "generation"}}}
    manifest.update(manifest_changes)
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return folder


def _errors(folder) -> str:
    return " | ".join(f.message for f in read_set(folder, strict=False).report.errors)


def test_example_set_passes():
    acs = read_set(EXAMPLE)
    assert acs.report.errors == [] and acs.report.warnings == []
    assert acs.components_of("capacity") == ["generation_capacity", "transmission", "distribution"]


def test_set_rejections(tmp_path):
    folder = _small_set(tmp_path)
    hourly = pd.read_csv(folder / "hourly.csv")
    hourly.drop(index=[100]).to_csv(folder / "hourly.csv", index=False)
    assert "hours of 2026-2026 missing" in _errors(folder)
    hourly.assign(stray=1.0).to_csv(folder / "hourly.csv", index=False)
    assert "have no class" in _errors(folder)
    hourly.assign(timestamp=hourly["timestamp"].str[:19]).to_csv(folder / "hourly.csv", index=False)
    assert "UTC offset" in _errors(folder)
    hourly.to_csv(folder / "hourly.csv", index=False)
    capacity = pd.read_csv(folder / "capacity.csv")
    capacity.assign(m7=0.5).to_csv(folder / "capacity.csv", index=False)
    assert "don't sum to 1" in _errors(folder)
    capacity.assign(price_per_kw_year=-1).to_csv(folder / "capacity.csv", index=False)
    assert "negative price" in _errors(folder)
    with pytest.raises(SetError):
        read_set(folder)


def test_manifest_rules_and_unit_warning(tmp_path):
    folder = _small_set(tmp_path, components={"energy": {"class": "energy"}, "generation_capacity": {"class": "capacity"}})
    assert "needs capacity_component" in _errors(folder)
    shutil.rmtree(folder)
    warned = read_set(_small_set(tmp_path, energy=45.0), strict=False)  # $/MWh by mistake
    assert any("$/MWh" in f.message for f in warned.report.warnings)


def test_year_local_and_extension(tmp_path):
    acs = read_set(EXAMPLE)
    y2026 = acs.year_local(2026)
    assert len(y2026) == 8760 and not y2026.isna().any().any()
    # beyond 2028, the 2028 values escalated 2%/yr, matched by date
    y2030 = acs.year_local(2030)
    assert y2030.loc["2030-07-15 18:00", "energy"] == pytest.approx(acs.year_local(2028).loc["2028-07-15 18:00", "energy"] * 1.02 ** 2)
    price, shares = acs.capacity_price(2030, "generation")
    assert price == pytest.approx(100 * 1.02 ** 2 * 1.02 ** 2) and shares.sum() == pytest.approx(1)
    small = read_set(_small_set(tmp_path))
    with pytest.raises(ValueError, match="declare `extend`"):
        small.year_local(2027)


def _flat(kw, index):
    return pd.Series(kw, index=index)


def test_screening_values(tmp_path):
    acs = read_set(_small_set(tmp_path))
    index = pd.DatetimeIndex(TimeGrid(60, 48, datetime(2026, 7, 1)).timestamps())
    without = _flat(np.r_[np.full(24, 1.0), np.zeros(24)], index)
    with_ = _flat(np.r_[np.zeros(24), np.full(24, 0.5)], index)  # lowers 24 kWh, then raises 12 kWh
    frame = screening_values(acs, without, with_, 1.0, {"R2/import/counted": pd.Series({7: 2.0})})
    energy = frame[frame["category"] == "energy"].set_index("part")["value"]
    assert energy["load lowered"] == pytest.approx(24 * 0.10) and energy["load raised"] == pytest.approx(-12 * 0.10)
    full = frame[frame["category"] == "full_hourly"]["value"].sum()
    assert full == pytest.approx(12 * 0.11)  # every component, comparison only
    capacity = frame[frame["category"] == "dr_capacity"]
    assert capacity["value"].sum() == pytest.approx(100.0 * 2.0)  # July share 1.0 x 2 kW, from capacity.csv only
    assert frame[frame["category"] == "emissions"]["value"].sum() == pytest.approx(12 / 1000 * 0.4)


def test_energy_impact_is_in_standard_time():
    grid = TimeGrid(60, 8760, datetime(2026, 1, 1))
    index = pd.DatetimeIndex(grid.timestamps())
    hour = index.hour.to_numpy()

    def result(net):
        s = pd.Series(net, index=index)
        return DispatchResult("x", index, pd.DataFrame(index=index), s.clip(lower=0), (-s).clip(lower=0), 0.0, "ok")

    without, with_ = result(np.full(8760, 2.0)), result(np.where(hour == 1, 0.0, 2.0))  # 2 kWh less at 1 am
    impact = energy_impact(without, with_, grid, TZ)
    assert len(impact) == 8760 and impact["timestamp"].str.endswith("-08:00").all()
    _, prevailing, _ = standard_time_hours(2026, TZ)
    # November's repeated 1 am is two real hours: the impact appears twice; March's
    # skipped 2 am appears never (no 1 am impact is lost there)
    assert impact["load_reduction_kwh"].sum() == pytest.approx(2.0 * (365 + 1))
    short = TimeGrid(60, 8000, datetime(2026, 1, 1))
    short_index = pd.DatetimeIndex(short.timestamps())
    part = DispatchResult("x", short_index, pd.DataFrame(index=short_index), pd.Series(1.0, index=short_index),
                          pd.Series(0.0, index=short_index), 0.0, "ok")
    with pytest.raises(ValueError, match="all of 2026"):
        energy_impact(part, part, short, TZ)


def test_cli_writes_screening_and_impact_package(tmp_path):
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["run", str(REPO / "examples/scenarios/dr_battery_annual.yaml"), "-o", str(out)])
    assert result.exit_code == 0, result.stdout
    assert "not cost-test results" in result.stdout and "<- valued" in result.stdout
    assert (out / "avoided_costs.csv").exists()
    package = out / "measure_impacts"
    for name in ("measure.yaml", "energy_impact.csv", "capacity.csv", "dr_incentives.csv"):
        assert (package / name).exists(), name
    measure = yaml.safe_load((package / "measure.yaml").read_text(encoding="utf-8"))
    assert measure["scope"] == "control" and measure["capacity_value"] == "R2/import/counted"
    assert measure["first_year"]["dr_incentive"] == pytest.approx(49.74, abs=0.01)
    assert len(pd.read_csv(package / "energy_impact.csv")) == 8760
