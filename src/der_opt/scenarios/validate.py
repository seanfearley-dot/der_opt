"""`der-opt validate`: every check on a scenario that doesn't need a solve
(docs/SCENARIO_COMPOSITION_SPEC.md section 4). `der-opt run` runs these first, so a
typo stops a run in seconds instead of after a long solve, and reuses what they built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from der_opt.diagnostics import CheckReport, Finding, collect_data_warnings
from der_opt.scenarios.compose import (
    PATH_KEYS,
    ComposedScenario,
    CompositionError,
    compose_scenario,
    key_path,
)

PLACEHOLDER = "TODO"


@dataclass
class Validation:
    path: Path
    report: CheckReport = field(default_factory=CheckReport)
    composed: ComposedScenario | None = None
    config: object | None = None  # ScenarioConfig or FeederScenarioConfig
    premise: object | None = None  # the built Premise (single-premise scenarios)
    feeder: object | None = None  # the built FeederScenario

    @property
    def ok(self) -> bool:
        return not self.report.errors

    @property
    def is_feeder(self) -> bool:
        return self.composed is not None and "feeder" in self.composed.data

    def fail(self, check: str, message: str) -> None:
        self.report.ran(check, [Finding("error", check, message)])


def _walk(data, trail: str = ""):
    """(dotted key, key, value) for every value in the scenario."""
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = enumerate(data)
    else:
        return
    for key, value in items:
        where = key_path(trail, key)
        yield where, (None if isinstance(key, int) else key), value
        yield from _walk(value, where)


def _placeholders(composed: ComposedScenario) -> list[Finding]:
    """Every TODO left in the merged scenario, naming the file it's written in."""
    import yaml

    holders = {}
    for path in [*composed.fragments, composed.path]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for where, _, value in _walk(raw):
            if isinstance(value, str) and PLACEHOLDER in value:
                holders[where] = path
    found = []
    for where, _, value in _walk(composed.data):
        if isinstance(value, str) and PLACEHOLDER in value:
            source = holders.get(where)
            found.append(Finding("error", "placeholders", f"{where} is still a placeholder ({value!r})"
                                 + (f" in {source.name}" if source else "") + "; fill it in"))
    return found


def _named_files(data) -> list[tuple[str, str]]:
    found = []
    for where, key, value in _walk(data):
        if key in PATH_KEYS and isinstance(value, str):
            found.append((where, value))
        elif key in PATH_KEYS and isinstance(value, list) and all(isinstance(v, str) for v in value):
            found.extend((f"{where}[{i}]", v) for i, v in enumerate(value))
    return [(w, v) for w, v in found if "://" not in v]


def _spec_files(data) -> list[tuple[str, str | None, str | None]]:
    """(file, premises file, network file) for every input-spec file the scenario reads."""
    files = []
    for _, _, value in _walk(data):
        if isinstance(value, dict) and "timeseries" in (value.get("source"), value.get("type")) and value.get("path"):
            files.append((value["path"], None, None))
    pdata = data.get("premise_data")
    if isinstance(pdata, dict):
        premises, network = pdata.get("premises"), pdata.get("network")
        for ts in pdata.get("timeseries") or []:
            files.append((ts, premises, network))
        if premises:
            files.append((premises, None, network))
        if network:
            files.append((network, None, None))
    return list(dict.fromkeys(files))


def _check_spec_files(v: Validation, quality: bool) -> None:
    from der_opt.inputs import check_file

    found = []
    for path, premises, network in _spec_files(v.composed.data):
        _, report, _ = check_file(path, premises=premises, network=network)
        found += [Finding(f.level, "input data files", f"{Path(path).name}: {f.check}: {f.message}", premise=f.premise)
                  for f in report.findings if f.level == "error" or (quality and f.level == "warning")]
    v.report.ran("input data files", found)


def _check_avoided_costs(v: Validation) -> None:
    from der_opt.avoided_costs import read_set

    config = v.config
    set_path = (config.cost_test.avoided_costs.set if getattr(config, "cost_test", None) is not None
                else config.avoided_costs.set if getattr(config, "avoided_costs", None) is not None else None)
    if set_path is None:
        return
    acs = read_set(set_path, strict=False)
    found = [Finding(f.level, "avoided cost set", f"{Path(set_path).name}: {f.message}")
             for f in acs.report.findings if f.level != "info"]
    test = config.cost_test
    if test is not None and not acs.report.errors:
        for year in range(test.start_year, test.start_year + test.measure_life_years):
            try:
                acs._escalation(year)
            except ValueError as exc:
                found.append(Finding("error", "avoided cost set", f"the measure's life runs to "
                                     f"{test.start_year + test.measure_life_years - 1}, but {exc}"))
                break
    v.report.ran("avoided cost set", found)


def _check_devices(v: Validation) -> None:
    config = v.config
    names = {d.name for d in config.devices}
    found = []
    for section, devices in (("dr.measure_devices", config.dr.measure_devices if config.dr else None),
                             ("cost_test.measure_devices", config.cost_test.measure_devices if config.cost_test else None)):
        unknown = sorted(set(devices or []) - names)
        if unknown:
            found.append(Finding("error", "device names", f"{section} names {unknown}, which aren't devices in this "
                                 f"scenario ({sorted(names)})"))
    v.report.ran("device names", found)


def _check_billing(v: Validation) -> None:
    from der_opt.diagnostics import check_tariff_dates
    from der_opt.scenarios.loader import build_billing

    billing = build_billing(v.config)
    if billing is None:
        return
    tariff = billing[0]
    found = []
    if tariff.baseline is not None and not v.config.billing.baseline_territory:
        found.append(Finding("error", "billing", f"{tariff.name} has a baseline credit; set billing.baseline_territory "
                             "(e.g. X)"))
    v.report.ran("billing", found)
    v.report.extend(check_tariff_dates(tariff, v.premise.time_grid))


def _check_dr(v: Validation) -> None:
    from der_opt.dr.calendar import CalendarError
    from der_opt.dr.study import _calendars, _without_measure

    if v.config.dr is None:
        return
    found = []
    try:
        calendars, _ = _calendars(v.config, v.premise.time_grid)
        if not any(calendars):
            found.append(Finding("warning", "dr calendar", "the event calendar has no events in this run"))
    except CalendarError as exc:
        found += exc.findings
    except ValueError as exc:
        found.append(Finding("error", "dr calendar", str(exc)))
    try:
        _without_measure(v.config)
    except ValueError as exc:
        found.append(Finding("error", "dr calendar", str(exc)))
    v.report.ran("dr calendar", found)


def _check_cost_test(v: Validation, cost_tests: bool, check_openbca: bool) -> None:
    from der_opt.settings import openbca_dir

    if v.config.cost_test is None:
        return
    level = "error" if cost_tests else "warning"
    found = []
    if v.config.cost_test.discount_rates is None:
        found.append(Finding(level, "cost test", "cost_test.discount_rates (utility, participant, societal) are "
                             "needed to run the cost tests in OpenBCA"))
    try:
        if check_openbca:
            openbca_dir()
    except FileNotFoundError as exc:
        found.append(Finding(level, "cost test", f"{exc} (needed for --cost-tests and `der-opt openbca`)"))
    v.report.ran("cost test", found)


def validate_scenario(path: str | Path, cost_tests: bool = False, build: bool = True,
                      before_run: bool = False) -> Validation:
    """Runs every no-solve check on a scenario. With cost_tests, a missing OpenBCA or
    missing discount rates are errors rather than warnings. before_run (what `der-opt
    run` uses) leaves out what only `der-opt validate` reports: `check-data`'s
    data-quality warnings on input-spec files (flat runs, outliers; their errors still
    count) and, without cost_tests, whether OpenBCA is set up."""
    from der_opt.diagnostics import check_load, check_prices
    from der_opt.scenarios.feeder import build_feeder_scenario
    from der_opt.scenarios.loader import build_premise
    from der_opt.scenarios.schema import FeederScenarioConfig, ScenarioConfig

    v = Validation(Path(path))
    try:
        v.composed = compose_scenario(path)
    except CompositionError as exc:
        v.fail("scenario file", str(exc))
        return v
    v.report.ran("scenario file")
    v.report.ran("paths", [Finding("warning", "paths", note) for note in v.composed.path_warnings])

    placeholders = _placeholders(v.composed)
    v.report.ran("placeholders", placeholders)
    if placeholders:
        return v

    schema = FeederScenarioConfig if v.is_feeder else ScenarioConfig
    try:
        v.config = schema.model_validate(v.composed.data)
    except ValidationError as exc:
        v.report.ran("schema", [Finding("error", "schema", f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
                                for e in exc.errors()])
        return v
    v.report.ran("schema")

    missing = [Finding("error", "files", f"{where}: {value} doesn't exist")
               for where, value in _named_files(v.composed.data) if not Path(value).exists()]
    v.report.ran("files", missing)
    if missing:
        return v
    _check_spec_files(v, quality=not before_run)
    if not v.is_feeder:
        _check_avoided_costs(v)
        _check_devices(v)
    if v.report.errors or not build:
        return v

    try:
        with collect_data_warnings(v.report):
            if v.is_feeder:
                v.feeder = build_feeder_scenario(v.config)
            else:
                v.premise = build_premise(v.config)
    except Exception as exc:  # noqa: BLE001 -- anything the build rejects is a scenario error
        v.fail("build", f"{type(exc).__name__}: {exc}")
        return v
    v.report.ran("build")
    if v.is_feeder:
        return v
    v.report.extend(check_prices(v.premise))
    v.report.extend(check_load(v.premise))
    _check_billing(v)
    _check_dr(v)
    _check_cost_test(v, cost_tests, check_openbca=cost_tests or not before_run)
    return v
