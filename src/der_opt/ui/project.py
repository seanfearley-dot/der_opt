"""What the app shows: the scenario and study files in a project folder, and the runs
written under it (docs/REPORTS.md section 3). Everything is read from the files; the
app keeps no database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from der_opt.results.summary import read_summary
from der_opt.scenarios.compose import CompositionError, compose_scenario
from der_opt.scenarios.schema import is_study

SKIP = {".git", ".venv", "venv", "node_modules", "_assets", "__pycache__", "out", "tests", ".der-opt"}
SCENARIO_KEYS = {"devices", "external_load", "feeder", "houses", "premise_data", "population", "premises", "analyses"}


class OutsideProject(ValueError):
    """A path that isn't inside the project folder."""


@dataclass
class ScenarioFile:
    path: Path  # relative to the project
    kind: str  # "study", "feeder" or "scenario"
    name: str
    modified: float


@dataclass
class Run:
    path: Path  # the output folder, relative to the project
    kind: str
    name: str
    created: str
    errors: int
    warnings: int
    has_report: bool


class Project:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def inside(self, relative: str | Path) -> Path:
        """`relative` as an absolute path, refusing anything outside the project."""
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise OutsideProject(f"{relative} isn't inside {self.root}")
        return path

    def relative(self, path: str | Path) -> Path:
        return Path(path).resolve().relative_to(self.root)

    def _walk(self, pattern: str, max_depth: int):
        def visit(folder: Path, depth: int):
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.is_dir():
                    if entry.name in SKIP or entry.name.startswith(".") or depth >= max_depth:
                        continue
                    yield from visit(entry, depth + 1)
                elif entry.match(pattern):
                    yield entry

        yield from visit(self.root, 0)

    def scenario_files(self) -> list[ScenarioFile]:
        found = []
        for path in self._walk("*.yaml", 5):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or "name" not in data:
                continue
            if not (SCENARIO_KEYS & set(data)) and "extends" in data:  # all of it in blocks: merge them to see
                try:
                    data = {**compose_scenario(path).data, "name": data["name"]}
                except (CompositionError, OSError, yaml.YAMLError):
                    continue
            if not (SCENARIO_KEYS & set(data)):
                continue
            kind = "study" if is_study(data) else "feeder" if "feeder" in data else "scenario"
            found.append(ScenarioFile(self.relative(path), kind, str(data["name"]), path.stat().st_mtime))
        return found

    def runs(self, folder: str = "out", max_depth: int = 4) -> list[Run]:
        """Output folders with a summary.json, leaving out the runs inside a study."""
        base = self.root / folder
        if not base.is_dir():
            return []
        found = []
        for summary_path in sorted(base.rglob("summary.json")):
            run = summary_path.parent
            if len(run.relative_to(base).parts) > max_depth:
                continue
            if any((parent / "summary.json").exists() for parent in run.parents if base in parent.parents or
                   parent == base):
                continue  # a study's feeder or portfolio premise
            summary = read_summary(run) or {}
            checks = summary.get("checks") or {}
            found.append(Run(self.relative(run), summary.get("kind", "?"), summary.get("name", run.name),
                             summary.get("created", ""), checks.get("errors", 0), checks.get("warnings", 0),
                             (run / "report.html").exists()))
        return sorted(found, key=lambda r: r.created, reverse=True)

    def default_output(self, scenario: str | Path) -> Path:
        return Path("out") / Path(scenario).stem
