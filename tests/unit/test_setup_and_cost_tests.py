"""`der-opt setup openbca`, where der-opt finds OpenBCA, and `der-opt run --cost-tests`.
Settings go to a temporary file; git and uv are simulated, so nothing touches the
network or your home directory. The end-to-end test needs OPENBCA_DIR."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from der_opt import settings
from der_opt.cli.main import app

REPO = Path(__file__).parent.parent.parent
runner = CliRunner()


def _flat(output: str) -> str:
    """CLI output without the error box's borders and line wraps."""
    return " ".join(output.replace("│", " ").replace("╭", " ").replace("╰", " ").split())


@pytest.fixture(autouse=True)
def private_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DER_OPT_CONFIG", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.delenv("DER_OPT_OPENBCA", raising=False)


def _fake_checkout(path: Path) -> Path:
    (path / "core").mkdir(parents=True)
    return path


def test_openbca_dir_lookup_order(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="der-opt setup openbca"):
        settings.openbca_dir()
    remembered, env, explicit = (_fake_checkout(tmp_path / n) for n in ("remembered", "env", "explicit"))
    settings.save_settings({"openbca": {"path": str(remembered)}})
    assert settings.openbca_dir() == remembered
    monkeypatch.setenv("DER_OPT_OPENBCA", str(env))
    assert settings.openbca_dir() == env
    assert settings.openbca_dir(explicit) == explicit
    with pytest.raises(FileNotFoundError, match="isn't an OpenBCA checkout"):
        settings.openbca_dir(tmp_path / "nowhere")


def test_setup_clones_pins_installs_and_remembers(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, cwd=None, capture_output=None, text=None):
        calls.append([str(c) for c in command])
        if command[:2] == ["git", "clone"]:
            _fake_checkout(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(settings, "find_uv", lambda: "/usr/local/bin/uv")
    target = tmp_path / "openbca"
    result = runner.invoke(app, ["setup", "openbca", "--dir", str(target)])
    assert result.exit_code == 0, result.stdout
    assert calls[0][:2] == ["git", "clone"] and calls[0][-1] == str(target.resolve())
    assert calls[1] == ["git", "-C", str(target.resolve()), "checkout", "--quiet", "--detach",
                        settings.OPENBCA_TESTED_COMMIT]
    assert calls[2] == ["/usr/local/bin/uv", "sync", "--quiet"]
    saved = yaml.safe_load(settings.config_path().read_text(encoding="utf-8"))
    assert saved["openbca"] == {"path": str(target.resolve()), "commit": settings.OPENBCA_TESTED_COMMIT}


def test_setup_refuses_a_folder_that_isnt_openbca(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "find_uv", lambda: "uv")
    occupied = tmp_path / "busy"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("mine", encoding="utf-8")
    result = runner.invoke(app, ["setup", "openbca", "--dir", str(occupied)])
    assert result.exit_code != 0 and "isn't an OpenBCA checkout" in _flat(result.output)


def test_setup_needs_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "find_uv", lambda: None)
    result = runner.invoke(app, ["setup", "openbca", "--dir", str(tmp_path / "x")])
    assert result.exit_code != 0 and "uv isn't installed" in _flat(result.output)


def test_cost_tests_fail_fast_without_openbca_or_a_cost_test_section(tmp_path):
    scenario = REPO / "examples" / "scenarios" / "dr_battery_annual.yaml"
    result = runner.invoke(app, ["run", str(scenario), "--cost-tests", "-o", str(tmp_path / "out")])
    assert result.exit_code != 0 and "der-opt setup openbca" in _flat(result.output)
    assert not (tmp_path / "out").exists()  # stopped before solving anything
    plain = REPO / "tests" / "fixtures" / "scenario_basic.yaml"
    result = runner.invoke(app, ["run", str(plain), "--cost-tests"])
    assert result.exit_code != 0 and "cost_test section" in _flat(result.output)
    feeder = REPO / "examples" / "scenarios" / "feeder_ev_heat_wave.yaml"
    result = runner.invoke(app, ["run", str(feeder), "--cost-tests"])
    assert result.exit_code != 0 and "single-measure" in _flat(result.output)


@pytest.mark.skipif(not os.environ.get("OPENBCA_DIR"), reason="needs an OpenBCA checkout: set OPENBCA_DIR")
def test_one_command_runs_the_cost_tests(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(REPO / "examples/scenarios/dr_battery_annual.yaml"), "--cost-tests",
                                 "--openbca", os.environ["OPENBCA_DIR"], "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "SPM convention" in result.output and (out / "openbca" / "cost_tests.csv").exists()
