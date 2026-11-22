"""summary.json (docs/REPORTS.md section 1) and the OpenBCA working-file cleanup."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.cost_test.openbca import ExportedTest, remove_working_files
from der_opt.results.summary import SUMMARY_VERSION, jsonable, read_summary

REPO = Path(__file__).parent.parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "scenario_basic.yaml"
runner = CliRunner()


def test_jsonable_makes_plain_json():
    value = {"a": np.float64(1.5), "b": float("nan"), "c": pd.Timestamp("2026-07-13 20:00"), "d": [np.int64(3)],
             "e": pd.DataFrame({"x": [1.0, np.nan]})}
    plain = jsonable(value)
    assert json.loads(json.dumps(plain)) == {"a": 1.5, "b": None, "c": "2026-07-13T20:00:00", "d": [3],
                                             "e": [{"x": 1.0}, {"x": None}]}


def test_a_single_home_run_writes_its_summary(tmp_path):
    scenario = tmp_path / "cf.yaml"
    scenario.write_text(FIXTURE.read_text() + "\ncounterfactual:\n  policies:\n    battery: {type: self_consumption}\n")
    out = tmp_path / "out"
    assert runner.invoke(app, ["run", str(scenario), "-o", str(out)]).exit_code == 0
    summary = read_summary(out)
    assert summary["summary_version"] == SUMMARY_VERSION and summary["kind"] == "scenario"
    costs = summary["costs"]
    assert costs["value_of_optimization"] == costs["counterfactual"]["total"] - costs["optimal"]["total"]
    assert {d["name"] for d in summary["devices"]} >= {"battery"}
    assert summary["counterfactual_policies"] == {"battery": "self_consumption"}
    assert summary["time_grid"]["steps"] > 0 and summary["checks"]["errors"] == 0
    assert "device_dispatch.csv" in summary["files"] and "summary.json" not in summary["files"]


def test_a_study_and_its_feeder_write_summaries(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(REPO / "examples/scenarios/feeder_coordination.yaml"), "-o", str(out),
                                 "--no-counterfactual"])
    assert result.exit_code == 0, result.output
    study, feeder = read_summary(out), read_summary(out / "feeder")
    assert study["kind"] == "study" and study["premises"] == 6 and study["feeder"] == {"folder": "feeder"}
    assert study["population"]["premises_with"]["ev"] == 3
    assert feeder["kind"] == "feeder" and feeder["mix"]["tx_1"]["premises"] == 3
    assert feeder["background"] == ["strip_mall"]
    assert set(feeder["runs"]) >= {"coordinated", "independent"}
    assert feeder["loading"]["coordinated"]["tx_1"]["rating_kw"] == 16
    runs = feeder["coordination"]["runs"]
    assert runs["envelopes"]["mechanisms"] == ["envelopes"] and runs["combined"]["share_of_ideal"] > 0.9
    assert feeder["coordinated_vs_independent"]["of"] == 6


def test_openbca_working_files_are_removed_and_results_kept(tmp_path):
    folder = tmp_path / "TRC"
    for name, size in (("test.json", 10), ("output/jst_ratio.csv", 10), ("output/streams.csv", 10),
                       ("output/openbca.log", 10), ("output/openbca.db", 5000), ("data/avoided_costs_ts.csv", 9000),
                       ("input_project/config.yaml", 10), ("input_project/.cache/x/y", 100)):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    freed = remove_working_files([ExportedTest("TRC", folder)])
    assert freed == 5000 + 9000 + 10 + 100
    left = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert left == ["output/jst_ratio.csv", "output/openbca.log", "output/streams.csv", "test.json"]
    assert not (folder / "data").exists() and not (folder / "input_project").exists()
