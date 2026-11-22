"""Running a coordinated feeder study (docs/COORDINATION_SPEC.md sections 2-6).

Order of work:
  1. every customer's baseline: its own bill (`own_bill`) or its rules (`rule_based`);
     `independent` is all of them together;
  2. non-participants (and background load) are fixed flows from then on;
  3. `coordinated`: participants in one optimization, the ideal;
  4. `counterfactual`: every customer rule-based, as before;
  5. each run in `coordination.runs`: participants respond to its mechanisms, each on
     its own. A run with dr_events first needs its twin without events (the same
     mechanisms, or `independent`), whose loading triggers the events and whose
     history sets each participant's expected settlement baseline.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from der_opt.coordination.envelopes import Inputs, compute_envelopes, envelope_arrays
from der_opt.coordination.mechanisms import (
    EnvelopeLimit,
    SignalAdder,
    element_prices,
    participant_adder,
    violations,
)
from der_opt.coordination.rules import Context, RuleSimulator, to_event_steps
from der_opt.network import NetworkDispatchModel, element_flows, solve_independently
from der_opt.results.schema import DispatchResult

MECHANISMS = ("price_signal", "envelopes", "dr_events")


@dataclass
class MechanismRun:
    label: str
    mechanisms: tuple[str, ...]
    dispatches: dict[str, DispatchResult]
    payments: pd.DataFrame  # per participant
    envelopes: pd.DataFrame | None = None
    signal: pd.DataFrame | None = None  # $/kWh by element
    calendar: pd.DataFrame | None = None
    twin: str | None = None  # the run without events a DR run was triggered and settled against
    is_twin: bool = False


@dataclass
class CoordinationResult:
    runs: dict[str, MechanismRun] = field(default_factory=dict)  # in order, twins included


def _solve_with(premise, streams) -> DispatchResult:
    """The premise's own optimization with the mechanisms' streams added. The reported
    objective leaves the mechanisms out (they're reported as payments), so it re-prices
    against the premise's own bill like any run."""
    variant = copy.copy(premise)
    variant.value_streams = list(premise.value_streams) + list(streams)
    result = solve_independently([variant])[premise.name]
    imp, exp = result.grid_import.to_numpy(), result.grid_export.to_numpy()
    mechanisms = sum(stream.evaluate_numeric(imp, exp, premise.time_grid) for stream in streams)
    return dataclasses.replace(result, objective_value=result.objective_value - mechanisms)


def rule_policies(house_config, premise):
    from der_opt.scenarios.loader import build_counterfactual_policies

    return build_counterfactual_policies(house_config, premise) or {}


def baselines(scenario) -> dict[str, DispatchResult]:
    """Each customer with no program: own bill or rules."""
    from der_opt.simulation import CounterfactualSimulator

    out = {}
    own = [p for p in scenario.premises if scenario.roles[p.name].baseline_behavior == "own_bill"]
    out.update(solve_independently(own))
    for house_config, premise in zip(scenario.house_configs, scenario.premises, strict=True):
        if premise.name not in out:
            out[premise.name] = CounterfactualSimulator(premise, rule_policies(house_config, premise)).run()
    return {p.name: out[p.name] for p in scenario.premises}


def default_runs(config) -> dict[str, list[str]]:
    configured = [m for m in MECHANISMS if getattr(config, m) is not None]
    runs = {m: [m] for m in configured}
    if len(configured) > 1:
        runs["combined"] = configured
    return runs


def _dr_calendar(cfg, loading_pct: np.ndarray | None, time_grid):
    from der_opt.dr import calendar as cal

    program = cfg.program
    if cfg.trigger is not None:
        if loading_pct is None:
            raise ValueError(f"dr_events: {cfg.element} needs a rating for a loading trigger")
        return cal.loading_triggered(loading_pct, program, time_grid, cfg.trigger.above_pct, cfg.duration, cfg.hours)
    events = cfg.events
    if events is None:
        raise ValueError("dr_events needs a trigger or events (file, price, random or schedule)")
    given = [n for n in ("file", "price", "random", "schedule") if getattr(events, n) is not None]
    if len(given) != 1:
        raise ValueError(f"dr_events.events needs exactly one constructor; got {given or 'none'}")
    if events.file is not None:
        return cal.from_file(events.file, program, time_grid, cfg.tz)
    if events.schedule is not None:
        return cal.schedule(events.schedule, program, time_grid)
    from der_opt.scenarios.loader import _build_price_series

    if events.price is not None:
        return cal.price_based(_build_price_series(events.price.series).values(time_grid), program, time_grid,
                               events.price)
    price = _build_price_series(events.random.series).values(time_grid) if events.random.series else None
    return cal.randomized(program, time_grid, events.random, 0, price)


def _settlement(load: pd.Series, events, customer_class: str, fallback: pd.Series, index) -> tuple[pd.Series, set]:
    """The settlement baseline over the run (NaN outside events). Where the run is too
    short for the settlement rule's history (an event on its first day), the twin's own
    load stands in -- what the customer would have done -- and the event is noted."""
    from der_opt.dr.settlement import baseline_series, settlement_baselines

    series = baseline_series(settlement_baselines(load, events, customer_class), events, index)
    filled = set()
    for e in events:
        rows = (index >= e.start) & (index < e.end)
        if series[rows].isna().any():
            series[rows] = series[rows].fillna(fallback[rows])
            filled.add(e.event_id)
    return series, filled


def _load(result: DispatchResult, basis: str) -> pd.Series:
    net = result.grid_import - result.grid_export
    return net.clip(lower=0) if basis == "import" else net


def run_coordination(scenario, base: dict[str, DispatchResult], fixed, congestion: pd.DataFrame | None,
                     cost_network) -> CoordinationResult:
    from der_opt.dr.response import DREventResponse

    config = scenario.config.coordination
    feeder, roles = scenario.feeder, scenario.roles
    time_grid = scenario.premises[0].time_grid
    index = pd.DatetimeIndex(time_grid.timestamps())
    steps, dt = len(index), time_grid.dt_hours
    by_name = {p.name: p for p in scenario.premises}
    house_configs = {c.name: c for c in scenario.house_configs}
    participants = [p.name for p in scenario.premises if roles[p.name].participant]
    wanted = config.runs or default_runs(config)
    for name, mechs in wanted.items():
        missing = [m for m in mechs if getattr(config, m) is None]
        if missing:
            raise ValueError(f"coordination run {name!r} uses {missing}, which have no settings")
        if name in ("coordinated", "independent", "counterfactual"):
            raise ValueError(f"coordination run name {name!r} is taken")

    inputs = Inputs(
        participants,
        {"import_kw": {p: base[p].grid_import.to_numpy() for p in participants},
         "export_kw": {p: base[p].grid_export.to_numpy() for p in participants}},
        {"import_kw": {p: by_name[p].import_limit_kw for p in participants},
         "export_kw": {p: by_name[p].export_limit_kw for p in participants}},
        {p: roles[p].weight for p in participants},
    )
    result = CoordinationResult()
    memo: dict[frozenset, str] = {}
    labels = {frozenset(m): n for n, m in wanted.items()}

    def solve(mechs: frozenset, label: str, is_twin: bool = False) -> str:
        if mechs in memo:
            return memo[mechs]
        envelopes = signal = calendar = None
        twin = None
        mask_by = {p: np.zeros(steps, dtype=bool) for p in participants}
        expected_by: dict[str, np.ndarray] = {}
        events = []
        if "envelopes" in mechs:
            envelopes = compute_envelopes(config.envelopes, feeder, inputs, fixed, index, dt)
        if "price_signal" in mechs:
            signal = element_prices(config.price_signal, index, feeder, congestion)
        if "dr_events" in mechs:
            cfg = config.dr_events
            rest = mechs - {"dr_events"}
            if rest:
                twin = solve(rest, labels.get(rest, f"{label} (no events)"), is_twin=rest not in labels)
                twin_dispatch = result.runs[twin].dispatches
            else:
                twin, twin_dispatch = "independent", base
            element = feeder.element(cfg.element)
            flows = element_flows(feeder, twin_dispatch, scenario.background)
            loading = None if element.rating_kw is None else 100 * flows[cfg.element].to_numpy() / element.rating_kw
            events = _dr_calendar(cfg, loading, time_grid)
            from der_opt.dr.calendar import calendar_frame

            calendar = calendar_frame(events)
            mask = np.zeros(steps, dtype=bool)
            for e in events:
                mask |= np.asarray((index >= e.start) & (index < e.end))
            basis = cfg.program.settlement_basis
            without_history: set = set()
            twin_load = {}
            for p in participants:
                if p in feeder.premises_below(cfg.element):
                    mask_by[p] = mask
                    twin_load[p] = _load(twin_dispatch[p], basis)
                    expected, filled = _settlement(twin_load[p], events, cfg.customer_class, twin_load[p], index)
                    expected_by[p] = expected.to_numpy()
                    without_history |= filled
            calendar["baseline"] = ["twin load (run too short for the settlement history)"
                                    if e in without_history else "settlement rule" for e in calendar["event_id"]]

        dispatches, rows = {}, []
        for p in scenario.premises:
            name = p.name
            if name not in participants:
                dispatches[name] = base[name]
                continue
            role = roles[name]
            adder = participant_adder(signal, feeder, name) if signal is not None else None
            caps = envelope_arrays(envelopes, name, steps) if envelopes is not None else (None, None)
            if role.response == "rules":
                ctx = Context(mask_by[name], to_event_steps(mask_by[name]), adder, caps[0], caps[1])
                dispatch, _ = RuleSimulator(p, rule_policies(house_configs[name], p), role, ctx).run()
            else:
                streams = []
                if adder is not None:
                    streams.append(SignalAdder(adder, time_grid))
                if envelopes is not None:
                    streams.append(EnvelopeLimit(caps[0], caps[1], config.envelopes.violation_penalty_per_kwh, time_grid))
                if mask_by[name].any():
                    streams.append(DREventResponse(config.dr_events.program, mask_by[name], expected_by[name], time_grid))
                dispatch = _solve_with(p, streams) if streams else base[name]
            dispatches[name] = dispatch
            row = {"house": name, "signal_charges": 0.0, "dr_incentive": 0.0, "dr_shortfall_kwh": 0.0,
                   "envelope_over_import_kwh": 0.0, "envelope_over_export_kwh": 0.0}
            imp, exp = dispatch.grid_import.to_numpy(), dispatch.grid_export.to_numpy()
            if adder is not None and config.price_signal.paid:
                row["signal_charges"] = float((adder * (imp - exp)).sum() * dt)
            if envelopes is not None:
                over_i, over_e = violations(caps[0], caps[1], imp, exp)
                row["envelope_over_import_kwh"], row["envelope_over_export_kwh"] = over_i * dt, over_e * dt
            if mask_by[name].any():
                program = config.dr_events.program
                basis = program.settlement_basis
                own = _load(dispatch, basis)
                settled, _ = _settlement(own, events, config.dr_events.customer_class, twin_load[name], index)
                for e in events:
                    rows_e = np.asarray((index >= e.start) & (index < e.end))
                    if program.response == "incentive":
                        kwh = float(((settled[rows_e] - own[rows_e]) * dt).sum())
                        row["dr_incentive"] += program.incentive_per_kwh * max(0.0, kwh)
                    elif program.response == "commitment":
                        target = settled[rows_e] - program.committed_kw
                        row["dr_shortfall_kwh"] += float((np.clip(own[rows_e] - target, 0, None) * dt).sum())
                    else:
                        row["dr_shortfall_kwh"] += float((np.clip(imp[rows_e] - program.cap_kw, 0, None) * dt).sum())
            rows.append(row)
        payments = pd.DataFrame(rows).set_index("house") if rows else pd.DataFrame()
        result.runs[label] = MechanismRun(label, tuple(m for m in MECHANISMS if m in mechs), dispatches, payments,
                                          envelopes, signal, calendar, twin, is_twin)
        memo[mechs] = label
        return label

    for name, mechs in wanted.items():
        solve(frozenset(mechs), name)
    # listed runs first, in the order given; twins after the run that needed them
    ordered = {}
    for name in wanted:
        run = result.runs[name]
        if run.twin and result.runs.get(run.twin, None) is not None and result.runs[run.twin].is_twin:
            ordered[run.twin] = result.runs[run.twin]
        ordered[name] = run
    result.runs = ordered
    return result


def share_of_ideal(independent: float, run: float, coordinated: float) -> float | None:
    """(independent - run) / (independent - coordinated), or None when the ideal saves too
    little to divide by: under $1, or under 1% of the independent network cost."""
    gap = independent - coordinated
    if gap < max(1.0, 0.01 * abs(independent)):
        return None
    return (independent - run) / gap


def make_network(scenario, participants: list, fixed) -> NetworkDispatchModel:
    return NetworkDispatchModel(scenario.feeder, participants, scenario.network_streams, fixed_loads=fixed)


def check_coordination(scenario) -> list:
    """Findings for `der-opt validate` (docs/COORDINATION_SPEC.md section 6)."""
    from der_opt.coordination.roles import rule_problems
    from der_opt.diagnostics import Finding

    found = []
    roles, feeder = scenario.roles, scenario.feeder
    for premise in scenario.premises:
        role = roles[premise.name]
        explicit = scenario.explicit_rules.get(premise.name, {})
        for problem in rule_problems(premise.name, role, premise, explicit):
            found.append(Finding("error", "coordination", problem))
        if explicit and role.response == "optimized":
            found.append(Finding("warning", "coordination", f"{premise.name}: has rules, but response: optimized "
                                 "ignores them (set response: rules)"))
    config = scenario.config.coordination
    if config is None:
        return found
    participants = [p.name for p in scenario.premises if roles[p.name].participant]
    if not participants:
        found.append(Finding("warning", "coordination", "no customer is a participant, so every run is the same"))
    try:
        wanted = config.runs or default_runs(config)
    except ValueError as exc:
        return [*found, Finding("error", "coordination", str(exc))]
    if not wanted:
        found.append(Finding("error", "coordination", "coordination has no mechanism settings (price_signal, "
                             "envelopes or dr_events)"))
    for name, mechs in wanted.items():
        if name in ("coordinated", "independent", "counterfactual"):
            found.append(Finding("error", "coordination", f"run name {name!r} is taken"))
        for m in mechs:
            if getattr(config, m) is None:
                found.append(Finding("error", "coordination", f"run {name!r} uses {m}, which has no settings"))
    if config.dr_events is not None:
        cfg = config.dr_events
        try:
            element = feeder.element(cfg.element)
        except KeyError:
            found.append(Finding("error", "coordination", f"dr_events.element {cfg.element!r} isn't a network element"))
        else:
            if not [p for p in feeder.premises_below(cfg.element) if p in participants]:
                found.append(Finding("error", "coordination", f"dr_events: no participants below {cfg.element}"))
            if cfg.trigger is not None and element.rating_kw is None:
                found.append(Finding("error", "coordination", f"dr_events trigger: {cfg.element} has no rating to "
                                     "measure loading against"))
        if cfg.trigger is None and cfg.events is None:
            found.append(Finding("error", "coordination", "dr_events needs a trigger or events"))
    if config.envelopes is not None:
        for quantity, method in (("import", config.envelopes.import_), ("export", config.envelopes.export)):
            if method is None:
                continue
            if method.method == "weights":
                missing = [p for p in participants if roles[p].weight is None]
                if missing:
                    found.append(Finding("error", "coordination", f"envelopes {quantity}: method weights needs "
                                         f"envelope_weight for {missing[:5]}"))
            if method.method == "service_size":
                limit = "import_limit_kw" if quantity == "import" else "export_limit_kw"
                missing = [p.name for p in scenario.premises if roles[p.name].participant
                           and getattr(p, limit) is None]
                if missing:
                    found.append(Finding("error", "coordination", f"envelopes {quantity}: method service_size needs "
                                         f"{limit} (or poi_limit_kw) for {missing[:5]}"))
            if method.method == "fixed" and method.kw is None:
                found.append(Finding("error", "coordination", f"envelopes {quantity}: method fixed needs kw"))
            if method.method == "file" and method.path is None:
                found.append(Finding("error", "coordination", f"envelopes {quantity}: method file needs path"))
    if config.price_signal is not None:
        signal = config.price_signal
        if signal.source == "schedule" and not signal.schedule:
            found.append(Finding("error", "coordination", "price_signal source schedule needs schedule rules"))
        for rule in signal.schedule:
            try:
                feeder.element(rule.element)
            except KeyError:
                found.append(Finding("error", "coordination", f"price_signal schedule: {rule.element!r} isn't a "
                                     "network element"))
        if signal.source == "file" and signal.path is None:
            found.append(Finding("error", "coordination", "price_signal source file needs path"))
    return found
