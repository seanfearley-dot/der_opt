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
    feeder: object | None = None  # the built FeederScenario (a feeder scenario, or a study's feeder analysis)
    resolved: list | None = None  # a study's resolved premises
    portfolio: list | None = None  # a study's portfolio premises (population.portfolio.PortfolioPremise)
    additions: list | None = None  # what each of a study's add_devices entries chose

    @property
    def is_study(self) -> bool:
        from der_opt.scenarios.schema import is_study

        return self.composed is not None and is_study(self.composed.data)

    @property
    def ok(self) -> bool:
        return not self.report.errors

    @property
    def is_feeder(self) -> bool:
        return self.composed is not None and "feeder" in self.composed.data and not self.is_study

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
    # a study's template and tariff can be names (no .yaml), which composition made paths
    named = [(w, v) for w, v in found if not (w.rsplit(".", 1)[-1] in ("template", "tariff") and not Path(v).suffix)]
    return [(w, v) for w, v in named if "://" not in v]


def _spec_files(data) -> list[tuple[str, str | None, str | None]]:
    """(file, premises file, network file) for every input-spec file the scenario reads."""
    files = []
    for _, _, value in _walk(data):
        if isinstance(value, dict) and "timeseries" in (value.get("source"), value.get("type")) and value.get("path"):
            files.append((value["path"], None, None))
    pdata = data.get("premise_data") or data.get("population")
    if isinstance(pdata, dict):
        premises, network = pdata.get("premises"), pdata.get("network")
        for ts in pdata.get("timeseries") or []:
            files.append((ts, premises, network))
        if premises:
            files.append((premises, None, network))
        if pdata.get("devices"):
            files.append((pdata["devices"], premises, network))
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
    for section, devices in (("dr.measure_devices", config.dr.measure_devices if config.dr else None),):
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


def _check_ev_patterns(v: Validation) -> None:
    from der_opt.devices.ev_pattern import scenario_patterns

    found = [Finding(level, "ev pattern", message)
             for _, _, expanded in scenario_patterns(v.config.devices, v.premise.time_grid)
             for level, message in expanded.notes]
    if found or any("pattern" in d.params for d in v.config.devices):
        v.report.ran("ev pattern", found)


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


def _check_cases(v: Validation) -> None:
    """The comparison cases the run will make: what `cases` asks for must be makeable,
    and each case's own billing must build."""
    from der_opt.cases import CasesError, case_billing, opening_pair, planned
    from der_opt.scenarios.loader import build_billing

    found = []
    try:
        names = planned(v.config)
        opening_pair(v.config, names)
        run_billing = build_billing(v.config)
        for name in names:
            case_billing(v.config, name, run_billing)
    except CasesError as exc:
        found.append(Finding("error", "cases", str(exc)))
    except (ValueError, OSError) as exc:
        found.append(Finding("error", "cases", f"{type(exc).__name__}: {exc}"))
    v.report.ran("cases", found)


def _check_cost_test(v: Validation, cost_tests: bool, check_openbca: bool, openbca=None) -> None:
    from der_opt.settings import openbca_dir

    if v.config.cost_test is None:
        return
    level = "error" if cost_tests else "warning"
    found = []
    if v.premise is not None:  # the measure impact package is for whole calendar years
        import pandas as pd

        index = pd.DatetimeIndex(v.premise.time_grid.timestamps())
        year = index[0].year
        whole = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq=f"{v.premise.time_grid.resolution_minutes}min",
                              inclusive="left")
        if len(index) != len(whole) or index[0] != whole[0]:
            found.append(Finding("error", "cost test", f"cost_test needs a run covering all of {year} "
                                 f"(this one covers {index[0]} to {index[-1]})"))
    from der_opt.cost_test.without import WithoutError, device_modes, kind_of

    try:  # what each device is compared against (v1.3)
        modes, unmatched = device_modes([(d.name, kind_of(d.type)) for d in v.config.devices],
                                        v.config.cost_test.without)
        if unmatched:
            found.append(Finding("warning", "cost test", f"cost_test.without names {unmatched}, which match no device "
                                 "here by name or type"))
        gone = [name for name, m in modes.items() if m["mode"] == "removed"]
        if gone:
            found.append(Finding("warning", "cost test", f"the comparison home has no {', '.join(gone)}, so the "
                                 f"measure buys {'them' if len(gone) > 1 else 'it'}: participant_costs.upfront "
                                 f"(${v.config.cost_test.participant_costs.upfront:,.0f}) should include the installed "
                                 "cost"))
    except WithoutError as exc:
        found.append(Finding("error", "cost test", str(exc)))
    if v.config.cost_test.discount_rates is None:
        found.append(Finding(level, "cost test", "cost_test.discount_rates (utility, participant, societal) are "
                             "needed to run the cost tests in OpenBCA"))
    try:
        if check_openbca:
            openbca_dir(openbca)
    except FileNotFoundError as exc:
        found.append(Finding(level, "cost test", f"{exc} (needed for --cost-tests and `der-opt openbca`)"))
    v.report.ran("cost test", found)


def _population_findings(exc) -> list[Finding]:
    return [Finding("error", "population", problem.split(": ", 1)[-1], premise=problem.split(": ", 1)[0])
            for problem in exc.problems]


def _validate_study(v: Validation, cost_tests: bool, before_run: bool, openbca=None) -> None:
    """A study: its population, its feeder analysis built in full, and each portfolio
    premise built with its DR calendar and cost-test settings (section 4a)."""
    from der_opt.coordination.study import check_coordination
    from der_opt.population.compile import PopulationError, resolve_study
    from der_opt.population.portfolio import portfolio_premises
    from der_opt.population.report import population_problems
    from der_opt.scenarios.feeder import feeder_from_study
    from der_opt.scenarios.loader import build_premise

    study = v.config
    details: dict = {}
    try:
        with collect_data_warnings(v.report):
            v.resolved, _, _ = resolve_study(study, details)
            v.additions = details.get("add_devices")
            problems = population_problems(v.resolved)
            if problems:
                raise PopulationError(problems)
    except PopulationError as exc:
        v.report.ran("population", _population_findings(exc))
        return
    v.report.ran("population")
    if study.analyses.feeder is not None:
        try:
            with collect_data_warnings(v.report):
                v.feeder = feeder_from_study(study)
        except PopulationError as exc:
            v.report.ran("feeder analysis", _population_findings(exc))
        except Exception as exc:  # noqa: BLE001 -- anything the build rejects is a study error
            v.fail("feeder analysis", f"{type(exc).__name__}: {exc}")
        else:
            v.report.ran("feeder analysis", check_coordination(v.feeder))
    if study.analyses.portfolio is None:
        return
    premises, problems = portfolio_premises(study, v.resolved)
    v.portfolio = premises
    found = [Finding("error", "portfolio", p.split(": ", 1)[-1], premise=p.split(": ", 1)[0]) for p in problems]
    for item in premises:
        if item.config is None:
            found.append(Finding("info", "portfolio", item.note, premise=item.premise_id))
            continue
        sub = Validation(v.path, config=item.config, composed=v.composed)
        try:
            with collect_data_warnings(sub.report):
                sub.premise = build_premise(item.config)
        except Exception as exc:  # noqa: BLE001
            found.append(Finding("error", "portfolio", f"{type(exc).__name__}: {exc}", premise=item.premise_id))
            continue
        _check_billing(sub)
        _check_dr(sub)
        _check_cost_test(sub, cost_tests, check_openbca=False)
        cost = item.config.cost_test
        if cost is not None and cost.participant_costs.upfront == 0 and cost.participant_costs.annual_om == 0:
            found.append(Finding("warning", "portfolio", "the measure has no participant costs (no installed_cost "
                                 "or participant_upfront): it's priced at $0", premise=item.premise_id))
        found += [Finding(f.level, f.check, f.message, premise=item.premise_id) for f in sub.report.findings
                  if f.level != "info"]
    v.report.ran("portfolio", found)
    first = next((item.config for item in premises if item.config is not None), None)
    if first is not None:
        sub = Validation(v.path, config=first, composed=v.composed)
        _check_avoided_costs(sub)  # the set is the study's: once is enough
        v.report.extend(sub.report)
        if first.cost_test is not None:
            _check_cost_test_openbca(v, cost_tests, before_run, openbca)


def _check_cost_test_openbca(v: Validation, cost_tests: bool, before_run: bool, openbca=None) -> None:
    from der_opt.settings import openbca_dir

    if not (cost_tests or not before_run):
        return
    try:
        openbca_dir(openbca)
    except FileNotFoundError as exc:
        level = "error" if cost_tests else "warning"
        v.report.ran("cost test", [Finding(level, "cost test", f"{exc} (needed for --cost-tests)")])


OLD_NAMES = {
    "premise_data": "write a study: these files under `population`, and tariff_dir, programs, income_tier and "
                    "baseline_code under `tariffs` (`dir` for tariff_dir)",
    "houses": "write a study, with the houses under `premises` (the same fields; `element` as here)",
    "house_defaults": "in a study, value_streams is the `objective`; the other settings are a `defaults` entry "
                      "with no `where`; device_params.<type> and counterfactual policies are `defaults` entries "
                      "with `where: {type: ...}`; rules go on those entries as a device's `rules`",
}


def _old_names(v: Validation) -> list[Finding]:
    """The pre-study names, read for one release (docs/POPULATION_MODEL_SPEC.md section 6)."""
    data = v.composed.data
    found = [Finding("warning", "old names", f"{name} is the old form, read for one release: {advice} "
                     "(docs/POPULATION_MODEL_SPEC.md section 6)") for name, advice in OLD_NAMES.items() if name in data]
    if (data.get("feeder") or {}).get("background_load"):
        found.append(Finding("warning", "old names", "feeder.background_load is the old form, read for one release: "
                             "in a study, a premise with `kind: background` (written out under `premises`, or a "
                             "premises.csv row)"))
    return found


def validate_scenario(path: str | Path, cost_tests: bool = False, build: bool = True,
                      before_run: bool = False, openbca: str | Path | None = None) -> Validation:
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

    from der_opt.scenarios.schema import StudyConfig

    schema = StudyConfig if v.is_study else FeederScenarioConfig if v.is_feeder else ScenarioConfig
    try:
        v.config = schema.model_validate(v.composed.data)
    except ValidationError as exc:
        v.report.ran("schema", [Finding("error", "schema", f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
                                for e in exc.errors()])
        return v
    v.report.ran("schema")
    if v.is_feeder:
        v.report.ran("old names", _old_names(v))

    missing = [Finding("error", "files", f"{where}: {value} doesn't exist")
               for where, value in _named_files(v.composed.data) if not Path(value).exists()]
    v.report.ran("files", missing)
    if missing:
        return v
    _check_spec_files(v, quality=not before_run)
    if v.is_study:
        if not v.report.errors and build:
            _validate_study(v, cost_tests, before_run, openbca)
        return v
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
        from der_opt.population.compile import PopulationError

        if isinstance(exc, PopulationError):  # one finding per problem, each naming its premise
            v.report.ran("population", [Finding("error", "population", problem.split(": ", 1)[-1],
                                                 premise=problem.split(": ", 1)[0]) for problem in exc.problems])
        else:
            v.fail("build", f"{type(exc).__name__}: {exc}")
        return v
    v.report.ran("build")
    if v.is_feeder:
        from der_opt.coordination.study import check_coordination

        v.report.ran("coordination", check_coordination(v.feeder))
        return v
    v.report.extend(check_prices(v.premise))
    v.report.extend(check_load(v.premise))
    _check_ev_patterns(v)
    _check_billing(v)
    _check_dr(v)
    _check_cases(v)
    _check_cost_test(v, cost_tests, check_openbca=cost_tests or not before_run, openbca=openbca)
    return v
