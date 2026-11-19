"""What produced a result: the scenario as resolved, a fingerprint of every input file,
the code version, the package versions, and the checks. Written as run_record.json
next to a run's outputs so a number can be traced back -- and reproduced -- months
later, on another machine, after rates or data have changed."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime
from importlib import metadata
from pathlib import Path

from der_opt.diagnostics import CheckReport

PACKAGES = ["der-opt", "pyomo", "highspy", "pandas", "numpy", "pydantic", "pyyaml", "typer", "matplotlib"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _referenced_paths(value, found: dict[str, Path]) -> None:
    """Every string in the scenario that names an existing file or folder."""
    if isinstance(value, dict):
        for item in value.values():
            _referenced_paths(item, found)
    elif isinstance(value, list):
        if value and all(isinstance(v, (int, float)) for v in value):
            return  # a literal array
        for item in value:
            _referenced_paths(item, found)
    elif isinstance(value, str) and len(value) < 500 and ("/" in value or "\\" in value or "." in value):
        path = Path(value)
        try:
            if path.exists():
                found[value] = path
        except OSError:
            pass


def _git(*args: str) -> str | None:
    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _versions() -> dict[str, str | None]:
    versions = {}
    for package in PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def build_run_record(scenario_path: str | Path, raw_scenario: dict, report: CheckReport, results: dict) -> dict:
    scenario_path = Path(scenario_path)
    paths: dict[str, Path] = {}
    _referenced_paths(raw_scenario, paths)
    inputs = []
    for text, path in sorted(paths.items()):
        if path.is_file():
            inputs.append({"path": text, "bytes": path.stat().st_size, "sha256": _sha256(path)})
        else:
            inputs.append({"path": text, "folder": True, "note": "folder; contents not fingerprinted"})
    commit = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "scenario": {"path": str(scenario_path), "sha256": _sha256(scenario_path), "resolved": raw_scenario},
        "inputs": inputs,
        "code": {
            "git_commit": commit,
            "uncommitted_changes": bool(dirty) if dirty is not None else None,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packages": _versions(),
        },
        "results": results,
        "checks": {"summary": report.summary(), "ran": report.checks_run,
                   "findings": report.to_frame().to_dict(orient="records")},
    }


def write_run_record(output_dir: Path, scenario_path: str | Path, raw_scenario: dict, report: CheckReport, results: dict) -> Path:
    record = build_run_record(scenario_path, raw_scenario, report, results)
    path = Path(output_dir) / "run_record.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    (Path(output_dir) / "scenario.yaml").write_bytes(Path(scenario_path).read_bytes())
    return path
